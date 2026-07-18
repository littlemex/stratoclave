"""Admin surface for per-tenant VSR configuration (opaque-blob, loosely coupled).

Routes (feature-flagged on ``EXTERNAL_VSR_ENABLED`` + a configured bucket):

  GET    /api/mvp/admin/tenants/{tenant_id}/vsr-config        -> raw text (404 if unset)
  PUT    /api/mvp/admin/tenants/{tenant_id}/vsr-config        -> validate-via-VSR + store
  DELETE /api/mvp/admin/tenants/{tenant_id}/vsr-config        -> revert to default
  POST   /api/mvp/admin/tenants/{tenant_id}/vsr-config/validate -> dry-run "Check" proxy

``{tenant_id}`` may be a real tenant id or the reserved literal ``default``
(the org-wide fallback). AUTHZ:

  * ``default``       -> admin only (org-wide knob);
  * a real tenant id  -> the tenant OWNER (team_lead) or an admin
    (``require_tenant_owner`` semantics; unknown tenant => 404, enumeration
    defense).

LOOSE COUPLING: the body is raw text (``text/plain`` / ``application/yaml``),
never a per-field JSON model. Stratoclave does NOT parse the YAML — validation
is delegated to the pinned VSR's ``/v1/config/validate`` at save time. Schema
changes across VSR versions therefore need ZERO Stratoclave changes.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dynamo import TenantsRepository

from .authz import get_current_user, log_audit_event
from .deps import AuthenticatedUser
from .vsr import config_store as store

router = APIRouter(prefix="/api/mvp/admin/tenants", tags=["admin-vsr-config"])


# --------------------------------------------------------------------------
# Feature gate + authz.
# --------------------------------------------------------------------------

def _require_enabled() -> None:
    """404 (not 500) when the feature is off or the bucket is unconfigured, so
    the surface is invisible unless the operator has fully provisioned it."""
    if not store.config_admin_enabled():
        raise HTTPException(status_code=404, detail="vsr config not available")


def _authorize(tenant_id: str, user: AuthenticatedUser) -> None:
    """Allow: an admin for any id (incl. ``default``); a tenant owner for their
    OWN tenant. Everything else -> unified 404 (enumeration defense), matching
    ``require_tenant_owner``.

    ``default`` is admin-only: it is not a real tenant, so no team_lead can own
    it, and a non-admin must not read/alter the org-wide fallback."""
    # Shape-guard the id BEFORE it reaches DynamoDB (same charset/length as the
    # S3-key guard): an unbounded/garbage path param must never become a
    # GetItem lookup surface. A non-conforming id maps to the same uniform 404
    # (enumeration defense) — it cannot name a real tenant nor `default`.
    if not store._SAFE_ID.match(tenant_id or ""):
        raise HTTPException(status_code=404, detail="Tenant not found")
    if "admin" in user.roles:
        if tenant_id == store.DEFAULT_KEY:
            return
        if not TenantsRepository().get(tenant_id):
            raise HTTPException(status_code=404, detail="Tenant not found")
        return
    # Non-admin: only a team_lead owning THIS tenant. `default` can never match.
    if tenant_id == store.DEFAULT_KEY:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant = TenantsRepository().get(tenant_id)
    if (
        not tenant
        or tenant.get("team_lead_user_id") != user.user_id
        or "team_lead" not in user.roles
    ):
        raise HTTPException(status_code=404, detail="Tenant not found")


async def _read_body(request: Request) -> str:
    """Read the raw request body as text, capping at the blob size limit so a
    huge upload is rejected before it is buffered in full. FastAPI has already
    read the body by the time this runs, so we enforce the cap on the decoded
    length here and again inside ``put_config`` / ``validate_blob``."""
    raw = await request.body()
    if len(raw) > store.MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail="config too large")
    # Reject (do not silently substitute) invalid UTF-8 so the blob stored in
    # S3 is byte-identical to what the tenant submitted and the size check is
    # exact — errors="replace" would corrupt the stored config and let the
    # re-encoded length diverge from the raw length.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="config must be valid UTF-8 text")


def _map_store_error(exc: store.VsrConfigError) -> HTTPException:
    if isinstance(exc, store.ConfigTooLarge):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, store.ConfigRejected):
        return HTTPException(status_code=422,
                             detail={"reason": "vsr_rejected", "errors": exc.errors})
    if isinstance(exc, store.ValidatorUnavailable):
        # Do NOT relay the underlying exception text to the caller: it can carry
        # the VSR's internal URL/host (httpx error strings are version-dependent
        # and HTTPStatusError.str() embeds the full URL). The store already logs
        # the detail server-side; the client gets a static message.
        return HTTPException(status_code=503,
                             detail={"reason": "vsr_validate_unavailable",
                                     "message": "vsr validator unreachable"})
    return HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------
# Routes.
# --------------------------------------------------------------------------

@router.get("/{tenant_id}/vsr-config")
def get_vsr_config(
    tenant_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Raw config text for this tenant (or ``default``). 404 if none is stored
    (the UI then offers to create one / shows the default read-only)."""
    _require_enabled()
    _authorize(tenant_id, user)
    try:
        got = store.get_config(tenant_id)
    except store.VsrConfigError as exc:
        raise _map_store_error(exc)
    if got is None:
        raise HTTPException(status_code=404, detail="no vsr config for tenant")
    text, version_id = got
    headers = {"x-vsr-config-version": version_id} if version_id else {}
    return Response(content=text, media_type="application/yaml", headers=headers)


@router.put("/{tenant_id}/vsr-config")
async def put_vsr_config(
    tenant_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Validate the raw blob via the pinned VSR, then store it in S3. Rejects
    (never stores) an unvalidated blob. Returns the new S3 version id."""
    _require_enabled()
    _authorize(tenant_id, user)
    blob = await _read_body(request)
    try:
        version_id = store.put_config(tenant_id, blob)
    except store.VsrConfigError as exc:
        raise _map_store_error(exc)
    log_audit_event(
        event="vsr_config_set",
        actor_id=user.user_id,
        actor_email=user.email,
        target_id=tenant_id,
        target_type="vsr_config",
        details={"version_id": version_id, "bytes": len(blob.encode("utf-8"))},
    )
    return {"tenant_id": tenant_id, "version_id": version_id, "stored": True}


@router.delete("/{tenant_id}/vsr-config")
def delete_vsr_config(
    tenant_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Remove a tenant's override so it reverts to ``default`` at the VSR."""
    _require_enabled()
    _authorize(tenant_id, user)
    try:
        store.delete_config(tenant_id)
    except store.VsrConfigError as exc:
        raise _map_store_error(exc)
    log_audit_event(
        event="vsr_config_deleted",
        actor_id=user.user_id,
        actor_email=user.email,
        target_id=tenant_id,
        target_type="vsr_config",
    )
    return {"tenant_id": tenant_id, "deleted": True}


@router.post("/{tenant_id}/vsr-config/validate")
async def validate_vsr_config(
    tenant_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Dry-run: proxy the blob to the VSR's /validate WITHOUT storing it (the UI
    "Check" button). Same authz as PUT so it can't be used to probe the VSR
    outside a tenant's scope."""
    _require_enabled()
    _authorize(tenant_id, user)
    blob = await _read_body(request)
    try:
        store.validate_blob(blob)
    except store.VsrConfigError as exc:
        raise _map_store_error(exc)
    return {"valid": True}
