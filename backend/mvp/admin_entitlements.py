"""C4 — the entitlement store: per-model-family access grants, as their own
item type in the existing `stratoclave-user-tenants` table.

A grant is `(tenant_id, model_family, profile_scope)`, keyed
`user_id="ENTITLEMENT#{model_family}#{profile_scope}"`, `tenant_id={tenant_id}`
— a pinned key convention, so this module and its tests land on the same rows
without comparing notes. Deliberately its OWN item type rather than a field
folded into `CONFIG#ROUTING`: `admin_routing.py`'s routing-config item is
full-replace, so a grant living inside it could be created or erased by an
unrelated routing-config edit. Writing the routing config must not touch
grants, and writing a grant must not touch the routing config — that
separation is the whole reason this store exists apart from it.

The admin surface lives here (this module, named like `admin_routing.py`,
`admin_api_keys.py`, `admin_sso_invites.py`) rather than in a module named
after the noun it manages: `mvp/entitlements.py` would be the odd one out
against every other admin route module in this package.

No new table and no IaC: `dynamo.client.user_tenants_table_name()` is the same
name resolver `admin_routing.py`'s routing-config writer already uses, and
listing a tenant's grants reuses the table's existing `tenant-id-index` GSI
(narrowed to entitlement rows by their reserved `ENTITLEMENT#` prefix, so a
tenant's routing-config or membership rows on the same table are never
mistaken for one).

PR2 stores and validates this policy only. Nothing here is read from the
reserve/chat path — the eligibility predicate, the refusal codes, and listing
filters are all PR3's; this module only ever answers an admin request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from dynamo import TenantsRepository
from dynamo.client import get_dynamodb_resource, user_tenants_table_name

from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser
from .models import registry_entries

router = APIRouter(prefix="/api/mvp/admin/tenants", tags=["admin-entitlements"])

#: Response header: present only when a grant/revoke committed but the audit
#: event describing it could not be written, naming why. Follows
#: `mvp.task_tag.HDR_TASK_TAG_DROPPED`'s own convention exactly: something the
#: request could not do, that must never change the response's status (the
#: grant/revoke already committed — reporting failure would tell the caller
#: an operation failed that in fact succeeded, the same lie the settle path
#: refuses), and must not be invisible either. The log this module also
#: writes on the same failure is for the operator; this header is for the
#: caller, who has no other way to learn the audit trail is incomplete.
HDR_AUDIT_DROPPED = "x-sc-audit-dropped"

#: The one value `HDR_AUDIT_DROPPED` currently carries. A single reason
#: (rather than a taxonomy like task_tag's "reserved"/"grammar") because there
#: is only one way this fails: the write to the audit sink itself raised.
AUDIT_DROPPED_WRITE_FAILED = "write_failed"

# The reserved `user_id` prefix every entitlement row carries. Also the string
# `list_entitlements` filters the shared table's `tenant-id-index` GSI on, so
# a tenant's routing-config item (`user_id="CONFIG#ROUTING"`) or a real
# membership row (`user_id=<a Cognito sub>`) on the SAME table is never read
# back as a grant.
_ENTITLEMENT_PREFIX = "ENTITLEMENT#"


def _entitlement_pk(model_family: str, profile_scope: str) -> str:
    """The pinned key convention for a grant row, so this module and any
    other reader of this table land on the same row without either side
    restating it from a shared literal the other might spell differently."""
    return f"{_ENTITLEMENT_PREFIX}{model_family}#{profile_scope}"


def _table():
    return get_dynamodb_resource().Table(user_tenants_table_name())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EntitlementError(ValueError):
    """A refused grant input (unknown family/scope pair, or a `general`
    entry). Mapped to HTTP 400 by the routes below."""


class EntitlementStoreUnavailable(Exception):
    """A read or write of the entitlement store failed and nothing was
    read/written.

    Mapped to a retryable 503 by the routes — the SAME fail-closed convention
    `_pipeline.py` (~line 2550) already uses for an unreadable restrictive
    routing config: a tenant's entitlements only ever RESTRICT what it may
    reach, so an unreadable store is not evidence of absence, and answering
    with a 403 would tell a caller to request access they may already hold.
    """


@dataclass(frozen=True)
class Entitlement:
    """A grant row as this module's own callers see it (both the route layer
    and, later, PR3's eligibility check)."""

    tenant_id: str
    model_family: str
    profile_scope: str
    granted_at: str
    granted_by: str


def _to_entitlement(item: dict[str, Any]) -> Entitlement:
    return Entitlement(
        tenant_id=str(item.get("tenant_id") or ""),
        model_family=str(item.get("model_family") or ""),
        profile_scope=str(item.get("profile_scope") or ""),
        granted_at=str(item.get("granted_at") or ""),
        granted_by=str(item.get("granted_by") or ""),
    )


def _find_entry(model_family: str, profile_scope: str):
    """The one registry entry this `(model_family, profile_scope)` pair
    names, or `None`. PR1's loader already refuses a registry document where
    a pair names more than one entry (`mvp/models.py`'s own uniqueness
    check), so at most one can ever match."""
    for entry in registry_entries():
        if entry.model_family == model_family and entry.profile_scope == profile_scope:
            return entry
    return None


def validate_grant_target(model_family: str, profile_scope: str) -> None:
    """C4's two write-time refusals for a grant's target.

    A family/scope pair no registry entry has is rejected — there is no
    forward-declaration concept here, so a grant cannot name a pair the
    registry does not already carry. A pair whose entry is `access="general"`
    is rejected as meaningless: every tenant can already reach a general
    entry, so a grant naming one would restrict nothing.
    """
    entry = _find_entry(model_family, profile_scope)
    if entry is None:
        raise EntitlementError(
            f"no registry entry declares model_family={model_family!r} at "
            f"profile_scope={profile_scope!r}"
        )
    if entry.access == "general":
        raise EntitlementError(
            f"model_family={model_family!r} at profile_scope={profile_scope!r} "
            f"is access=general; every tenant can already reach it, so a "
            f"grant would be meaningless"
        )


def get_entitlement(
    tenant_id: str, model_family: str, profile_scope: str
) -> Optional[Entitlement]:
    """Read a single grant, consistently.

    Raises `EntitlementStoreUnavailable` on a failed read rather than
    answering `None` — an unreadable store is not evidence of absence (see
    that class's own docstring).
    """
    try:
        resp = _table().get_item(
            Key={
                "user_id": _entitlement_pk(model_family, profile_scope),
                "tenant_id": tenant_id,
            },
            ConsistentRead=True,
        )
    except ClientError as exc:
        raise EntitlementStoreUnavailable(
            f"entitlement store unreachable reading tenant {tenant_id!r} "
            f"model_family={model_family!r} profile_scope={profile_scope!r}: {exc}"
        ) from exc
    item = resp.get("Item")
    return _to_entitlement(item) if item else None


def list_entitlements(tenant_id: str) -> list[Entitlement]:
    """Every grant `tenant_id` holds, via the table's existing `tenant-id-
    index` GSI (no new table, no new index), narrowed to entitlement rows by
    their reserved `ENTITLEMENT#` prefix."""
    try:
        resp = _table().query(
            IndexName="tenant-id-index",
            KeyConditionExpression=(
                boto3_key("tenant_id").eq(tenant_id)
                & boto3_key("user_id").begins_with(_ENTITLEMENT_PREFIX)
            ),
        )
    except ClientError as exc:
        raise EntitlementStoreUnavailable(
            f"entitlement store unreachable listing tenant {tenant_id!r}: {exc}"
        ) from exc
    return [_to_entitlement(item) for item in resp.get("Items", [])]


def _emit_audit_after_commit(
    *, event: str, actor: AuthenticatedUser, tenant_id: str, model_family: str,
    profile_scope: str, before: Optional[dict[str, str]], after: Optional[dict[str, str]],
) -> Optional[str]:
    """Audit AFTER the write already committed (both call sites below).

    Returns `None` when the audit event was written, or
    `AUDIT_DROPPED_WRITE_FAILED` when it was not — never raises. A failed
    audit write must not roll back a grant/revoke that already committed —
    there is no state to roll back TO that is safer than the one already in
    force — so the failure is caught here rather than re-raised through the
    route, which would tell the caller their grant/revoke failed when it did
    not. It is still reported twice, to the two parties who can each act on
    it differently: logged (for the operator), and returned here so the
    caller (`grant_entitlement`/`revoke_entitlement`, and through them the
    route) can surface `HDR_AUDIT_DROPPED` on an otherwise-successful
    response — a caller has no other way to learn the trail is incomplete.

    `target_id` and `target_type` are pinned by name (`"{model_family}#
    {profile_scope}"` / `"entitlement"`) rather than left to "the triple is
    in the event somewhere": a value that is present but not under the name a
    reader looks for is indistinguishable from absent to that reader.
    """
    try:
        log_audit_event(
            event=event, actor_id=actor.user_id, actor_email=actor.email,
            target_id=f"{model_family}#{profile_scope}",
            target_type="entitlement", tenant_id=tenant_id,
            before=before, after=after,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed; see below.
        # Lazy, defensively-wrapped import: logging the failure must not
        # itself mask the failure (the same convention
        # `routing/config.py`'s `_log_read_fault` already uses).
        try:
            from core.logging import get_logger
            get_logger(__name__).warning(
                "entitlement_audit_write_failed", event=event, tenant_id=tenant_id,
                model_family=model_family, profile_scope=profile_scope, error=str(exc),
            )
        except Exception:  # noqa: BLE001 — logging must not mask the fault itself.
            pass
        return AUDIT_DROPPED_WRITE_FAILED


def grant_entitlement(
    *, tenant_id: str, model_family: str, profile_scope: str, actor: AuthenticatedUser,
) -> tuple[Entitlement, Optional[str]]:
    """Grant `(tenant_id, model_family, profile_scope)`.

    Idempotent: granting the same triple twice is a no-op that returns the
    EXISTING row — its own `granted_at`/`granted_by` — never a second write
    that reassigns provenance to whoever asked the second time, and never a
    second audit event describing a change that did not happen.

    Returns `(grant, audit_dropped_reason)`. `audit_dropped_reason` is `None`
    on every idempotent repeat (no write happened, so there is nothing to
    have failed to audit) and on a fresh grant whose audit event was written;
    it is `AUDIT_DROPPED_WRITE_FAILED` only when a real write committed and
    its audit event could not be. The grant itself is never refused or rolled
    back for this — see `_emit_audit_after_commit`.
    """
    validate_grant_target(model_family, profile_scope)
    pk = _entitlement_pk(model_family, profile_scope)
    item = {
        "user_id": pk,
        "tenant_id": tenant_id,
        "model_family": model_family,
        "profile_scope": profile_scope,
        "granted_at": _now_iso(),
        "granted_by": actor.user_id,
    }
    created = False
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(user_id)")
        created = True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise EntitlementStoreUnavailable(
                f"entitlement store unreachable granting tenant {tenant_id!r} "
                f"model_family={model_family!r} profile_scope={profile_scope!r}: {exc}"
            ) from exc
        existing = get_entitlement(tenant_id, model_family, profile_scope)
        if existing is not None:
            return existing, None
        # The row that just failed our condition is gone again — a revoke won
        # a race between the failed put and this read. The caller asked to
        # grant, so grant now unconditionally rather than surface a transient
        # inconsistency neither side did anything wrong to cause.
        _table().put_item(Item=item)
        created = True

    audit_dropped_reason = None
    if created:
        audit_dropped_reason = _emit_audit_after_commit(
            event="entitlement_granted", actor=actor, tenant_id=tenant_id,
            model_family=model_family, profile_scope=profile_scope,
            before=None,
            after={"granted_at": item["granted_at"], "granted_by": item["granted_by"]},
        )
    return _to_entitlement(item), audit_dropped_reason


def revoke_entitlement(
    *, tenant_id: str, model_family: str, profile_scope: str, actor: AuthenticatedUser,
) -> Optional[str]:
    """Revoke `(tenant_id, model_family, profile_scope)`.

    Revoking a grant that does not exist succeeds: `DeleteItem` with no
    condition is already this idempotent, so this is the natural shape rather
    than a read-then-delete that would have to special-case "already gone".
    `ReturnValues="ALL_OLD"` gets the pre-image atomically in the SAME call
    (the pattern `dynamo/ui_tickets.py`'s `consume()` already uses), so
    knowing whether anything actually changed — and therefore whether an
    audit event describing a real transition is warranted — costs no second
    read and no race against a concurrent grant/revoke of the same row.

    Returns the audit-dropped reason (`None`, or `AUDIT_DROPPED_WRITE_FAILED`)
    exactly like `grant_entitlement` — `None` on both "nothing existed to
    revoke" and "revoked, and the audit event was written".
    """
    pk = _entitlement_pk(model_family, profile_scope)
    try:
        resp = _table().delete_item(
            Key={"user_id": pk, "tenant_id": tenant_id},
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        raise EntitlementStoreUnavailable(
            f"entitlement store unreachable revoking tenant {tenant_id!r} "
            f"model_family={model_family!r} profile_scope={profile_scope!r}: {exc}"
        ) from exc
    # Checked by FALSINESS, not `is None`: a `DeleteItem` on a key that was
    # never there can come back with `Attributes` present but EMPTY (`{}`)
    # rather than the key omitted entirely (observed under moto; real
    # DynamoDB's own documented behaviour for `ReturnValues=ALL_OLD` is "the
    # content of the old item", which an absent item has none of) -- the same
    # defensive read `dynamo/ui_tickets.py`'s `consume()` already uses for
    # this exact response shape.
    old = resp.get("Attributes") or None
    if old is None:
        return None
    return _emit_audit_after_commit(
        event="entitlement_revoked", actor=actor, tenant_id=tenant_id,
        model_family=model_family, profile_scope=profile_scope,
        before={
            "granted_at": str(old.get("granted_at") or ""),
            "granted_by": str(old.get("granted_by") or ""),
        },
        after=None,
    )


# =============================================================================
# Admin routes
# =============================================================================
class EntitlementResponse(BaseModel):
    tenant_id: str
    model_family: str
    profile_scope: str
    granted_at: str
    granted_by: str


class EntitlementListResponse(BaseModel):
    # A single top-level key holding the list, named `grants` (not
    # `entitlements`, and with no `tenant_id` echo — the path already names
    # the tenant) so the shape can grow a sibling key later without breaking
    # a client that only reads this one.
    grants: list[EntitlementResponse]


def _require_tenant(tenant_id: str) -> None:
    if not TenantsRepository().get(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")


def _err_503_store_unavailable() -> HTTPException:
    # Same shape/convention as `_pipeline.py`'s `_err_503`: a `type` a client's
    # retry logic can key on, distinct from a 403 (see
    # `EntitlementStoreUnavailable`'s own docstring for why 403 is wrong here).
    return HTTPException(
        status_code=503,
        detail={
            "type": "entitlement_store_unavailable",
            "message": "The entitlement store is temporarily unavailable. Retry shortly.",
        },
    )


def _entitlement_response(grant: Entitlement) -> EntitlementResponse:
    return EntitlementResponse(
        tenant_id=grant.tenant_id, model_family=grant.model_family,
        profile_scope=grant.profile_scope, granted_at=grant.granted_at,
        granted_by=grant.granted_by,
    )


@router.get("/{tenant_id}/entitlements", response_model=EntitlementListResponse)
def list_tenant_entitlements(
    tenant_id: str,
    actor: AuthenticatedUser = Depends(require_permission("entitlements:read")),
) -> EntitlementListResponse:
    """Every grant this tenant holds. `entitlements:read` is separate from
    `entitlements:grant` (C17): seeing what a tenant may use is not the same
    authority as changing it."""
    _require_tenant(tenant_id)
    try:
        grants = list_entitlements(tenant_id)
    except EntitlementStoreUnavailable:
        raise _err_503_store_unavailable()
    return EntitlementListResponse(grants=[_entitlement_response(g) for g in grants])


@router.put(
    "/{tenant_id}/entitlements/{model_family}/{profile_scope}",
    response_model=EntitlementResponse,
)
def put_entitlement(
    tenant_id: str, model_family: str, profile_scope: str,
    response: Response,
    actor: AuthenticatedUser = Depends(require_permission("entitlements:grant")),
) -> EntitlementResponse:
    """Grant `(tenant_id, model_family, profile_scope)`. `PUT` rather than
    `POST`, and path-addressed rather than a body, because the verb should
    say what the contract already requires: putting the same triple twice is
    idempotent (it returns the existing grant rather than erroring or
    re-provisioning it), which is what `PUT` means and `POST` does not.

    Status stays 200 even when the grant committed but its audit event could
    not be written — the write itself succeeded, so reporting failure would
    be the same lie a settle-path 5xx over a committed charge would be.
    `HDR_AUDIT_DROPPED` carries that fact to the caller instead (`response` is
    FastAPI's own per-request Response object, injected as a parameter — the
    same mechanism `mvp.anthropic.messages()` already uses to set the
    correlation-id headers on a JSON body without changing what gets
    returned).
    """
    _require_tenant(tenant_id)
    try:
        grant, audit_dropped_reason = grant_entitlement(
            tenant_id=tenant_id, model_family=model_family,
            profile_scope=profile_scope, actor=actor,
        )
    except EntitlementError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except EntitlementStoreUnavailable:
        raise _err_503_store_unavailable()
    if audit_dropped_reason is not None:
        response.headers[HDR_AUDIT_DROPPED] = audit_dropped_reason
    return _entitlement_response(grant)


@router.delete("/{tenant_id}/entitlements/{model_family}/{profile_scope}")
def delete_entitlement(
    tenant_id: str, model_family: str, profile_scope: str,
    actor: AuthenticatedUser = Depends(require_permission("entitlements:grant")),
) -> Response:
    """Revoke `(tenant_id, model_family, profile_scope)`. Succeeds even when
    the grant does not exist (or never did) — revoking is idempotent in the
    same sense granting is.

    Same `HDR_AUDIT_DROPPED` treatment as the grant route above, on the same
    204. This handler builds its own `Response` rather than taking one as a
    parameter (there is no body to return), so the header is set directly on
    it before returning.
    """
    _require_tenant(tenant_id)
    try:
        audit_dropped_reason = revoke_entitlement(
            tenant_id=tenant_id, model_family=model_family,
            profile_scope=profile_scope, actor=actor,
        )
    except EntitlementStoreUnavailable:
        raise _err_503_store_unavailable()
    response = Response(status_code=204)
    if audit_dropped_reason is not None:
        response.headers[HDR_AUDIT_DROPPED] = audit_dropped_reason
    return response
