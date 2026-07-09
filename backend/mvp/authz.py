"""Phase 2 RBAC / Tenant authorization logic.

Follows design doc §3:
- DynamoDB Permissions table is the source of truth, seeded from permissions.json.
- Cognito Groups are not used; the `cognito:groups` claim is ignored.
- Wildcards match only on exact resource name (users:* covers users:create but not
  users-admin:create; resource names must not contain - or _).
- require_tenant_owner returns unified 404 for non-admins (enumeration defense).
- High-risk admin operations emit structured audit JSON to CloudWatch.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from fastapi import Depends, HTTPException

from dynamo import PermissionsRepository, TenantsRepository

from .deps import AuthenticatedUser, get_current_user


# -----------------------------------------------------------------
# Permissions cache (TTL 10 seconds, minimizes consistency lag across ECS tasks)
# -----------------------------------------------------------------
_PERMS_CACHE: dict[str, tuple[list[str], float]] = {}
_PERMS_TTL = 10.0


def _get_permissions_for_role(role: str) -> list[str]:
    now = time.time()
    cached = _PERMS_CACHE.get(role)
    if cached and cached[1] > now:
        return cached[0]
    perms = PermissionsRepository().get(role) or []
    _PERMS_CACHE[role] = (perms, now + _PERMS_TTL)
    return perms


def _clear_permissions_cache() -> None:
    """Primarily for use in tests."""
    _PERMS_CACHE.clear()


def _split_resource(permission: str) -> str:
    """Extract the resource portion (everything before the first colon) of a permission string."""
    return permission.split(":", 1)[0]


def has_permission(roles: Iterable[str], permission: str) -> bool:
    """Return True if any of the given roles grants the specified permission.

    - Wildcards: "users:*" covers "users:create" and "users:read",
      but only on an exact resource-name match (will not match "users-admin:*").
    - Multiple roles (e.g. ["admin", "team_lead"]) are evaluated as a union.
    """
    target_resource = _split_resource(permission)
    for role in roles:
        perms = _get_permissions_for_role(role)
        for p in perms:
            if p == permission:
                return True
            if p.endswith(":*"):
                p_resource = _split_resource(p)
                if p_resource == target_resource:
                    return True
    return False


def _permission_matches(perms: Iterable[str], permission: str) -> bool:
    """Check a permission directly against a list of permission strings (no role resolution).

    Unlike `has_permission(roles, ...)`, this bypasses the roles → Permissions table lookup
    and evaluates against a raw permissions list. Used for API Key scope checks.
    """
    target_resource = _split_resource(permission)
    for p in perms:
        if p == permission:
            return True
        if p.endswith(":*"):
            p_resource = _split_resource(p)
            if p_resource == target_resource:
                return True
    return False


def user_has_permission(user: AuthenticatedUser, permission: str) -> bool:
    """Evaluate a permission uniformly for both JWT (Cognito) and API Key (sk-stratoclave) auth.

    - JWT auth: returns True if user.roles grants the permission.
    - API Key auth: requires **both** user.roles and user.key_scopes to grant the permission
      (API Key scopes are effective only as a subset of the owner's role grants).
    """
    if user.auth_kind == "api_key" and user.key_scopes is not None:
        if not _permission_matches(user.key_scopes, permission):
            return False
        # Also verify the owner's roles include the permission.
        return has_permission(user.roles, permission)
    return has_permission(user.roles, permission)


# -----------------------------------------------------------------
# FastAPI dependency helpers
# -----------------------------------------------------------------
def require_permission(permission: str) -> Callable[..., AuthenticatedUser]:
    """FastAPI dependency that allows only users holding the specified permission.

    For API Key auth, requires both user.roles and user.key_scopes to grant
    the permission (evaluated via user_has_permission).
    """

    def _dep(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if not user_has_permission(user, permission):
            raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
        return user

    return _dep


def require_any_role(*allowed_roles: str) -> Callable[..., AuthenticatedUser]:
    """FastAPI dependency that allows only users holding at least one of the specified roles (coarser than permission checks)."""

    def _dep(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if not any(r in user.roles for r in allowed_roles):
            raise HTTPException(status_code=403, detail="Forbidden role")
        return user

    return _dep


def require_tenant_owner(tenant_id_param: str = "tenant_id") -> Callable[..., AuthenticatedUser]:
    """Allow only the tenant owner (team_lead_user_id == user.user_id) or an admin.

    Admins have access to all tenants.
    Non-owners and non-existent tenants both return a unified 404 (§3.7, enumeration defense).
    """

    def _dep(
        tenant_id: str,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        tenant = TenantsRepository().get(tenant_id)
        if "admin" in user.roles:
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            return user
        if not tenant or tenant.get("team_lead_user_id") != user.user_id:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return user

    # When the param name is "tenant_id", FastAPI injects the path parameter automatically.
    # For a different name, ensure the endpoint uses "tenant_id" as the path param name.
    _dep.__name__ = f"require_tenant_owner_{tenant_id_param}"
    return _dep


# -----------------------------------------------------------------
# Admin creation gate (Critical C-D) — P1-A hardening (2026-04 review)
# -----------------------------------------------------------------
#
# The first-admin bootstrap path opens `POST /api/mvp/admin/users` to
# anyone who can reach the backend, because the very first user has to
# be created before anyone can authenticate. The old flag
# `ALLOW_ADMIN_CREATION=true` was global and sticky: if an operator
# forgot to unset it after bootstrap, any unauthenticated caller could
# mint themselves an admin. That is the single biggest operational
# footgun in the current threat model.
#
# P1-A keeps the env var for dev ergonomics, but in `ENVIRONMENT=production`
# the flag alone is insufficient — the operator must also set
# `ALLOW_ADMIN_CREATION_UNTIL=<epoch seconds>` to a future instant. The
# gate auto-closes when `now > epoch`, so even "oops I shipped with the
# flag on" stops being a permanent exposure.
#
# Dev / staging keep the old sticky-flag behaviour so `stratoclave
# auth login` smoke tests don't grow a new ceremony. The warn log on
# every request (plus the startup warn) makes the state impossible to
# miss in CloudWatch.
def _is_production() -> bool:
    # Default to production when unset. `main.py` already fails closed
    # if ENVIRONMENT is missing in prod-critical env validation, but
    # centralising the default here means any helper that reads
    # ENVIRONMENT agrees with the rest of the backend.
    return os.getenv("ENVIRONMENT", "production").lower() == "production"


def _admin_creation_until_epoch() -> int:
    """Return the expiry epoch (seconds) for the admin bootstrap window,
    or 0 if unset / malformed. Callers interpret 0 as "no time-bound
    window configured" and combine it with the sticky boolean flag.
    """
    raw = os.getenv("ALLOW_ADMIN_CREATION_UNTIL", "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        # A malformed value fails closed in production: the sticky
        # boolean is not enough on its own there, and a bad number
        # here cannot satisfy the `now <= epoch` check.
        return 0


def admin_creation_allowed() -> bool:
    """Return True when `POST /api/mvp/admin/users` may create an admin.

    * Development / staging: the classic sticky `ALLOW_ADMIN_CREATION=true`
      still works, to keep local smoke tests ergonomic.
    * Production: the boolean is NOT enough. The operator must ALSO set
      `ALLOW_ADMIN_CREATION_UNTIL=<future-epoch>`; the gate auto-closes
      at that instant. A missing or past epoch means admin creation is
      denied even if the boolean is on.
    """
    flag = os.getenv("ALLOW_ADMIN_CREATION", "false").lower() == "true"
    if not flag:
        return False
    if not _is_production():
        return True
    until = _admin_creation_until_epoch()
    if until <= 0:
        return False
    import time

    return int(time.time()) <= until


_LAST_ADMIN_GATE_WARN_AT: float = 0.0
_ADMIN_GATE_WARN_INTERVAL_SECONDS: int = 300


def warn_if_admin_creation_enabled_in_production(logger: logging.Logger) -> None:
    """Emit an audit-level warning if the bootstrap gate is open.

    Called from `main.py` on startup and (if present) from the
    `create_user` handler. To avoid flooding CloudWatch with the same
    epoch on every request (A-11-log), the warning is rate-limited to
    once per ``_ADMIN_GATE_WARN_INTERVAL_SECONDS`` per process.
    """
    if not _is_production():
        return
    if not admin_creation_allowed():
        return
    import time

    global _LAST_ADMIN_GATE_WARN_AT
    now = time.time()
    if now - _LAST_ADMIN_GATE_WARN_AT < _ADMIN_GATE_WARN_INTERVAL_SECONDS:
        return
    _LAST_ADMIN_GATE_WARN_AT = now

    until = _admin_creation_until_epoch()
    remaining = max(0, until - int(now)) if until else 0
    logger.warning(
        "allow_admin_creation_enabled_in_production",
        extra={
            "event": "allow_admin_creation_warning",
            "environment": "production",
            "expires_at": until,
            "seconds_remaining": remaining,
        },
    )


# -----------------------------------------------------------------
# Audit log (structured JSON to CloudWatch; will be promoted to a dedicated table in Phase 3)
# -----------------------------------------------------------------
_audit_logger = logging.getLogger("stratoclave.audit")


def log_audit_event(
    *,
    event: str,
    actor_id: str,
    actor_email: Optional[str] = None,
    target_id: Optional[str] = None,
    target_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Emit an audit log entry for high-risk admin operations to CloudWatch.

    Example `event` values: "admin_created", "tenant_owner_changed",
    "user_tenant_switched", "credit_overwritten", "user_deleted".
    """
    payload: dict[str, Any] = {
        "event": event,
        "actor_id": actor_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if actor_email:
        payload["actor_email"] = actor_email
    if target_id:
        payload["target_id"] = target_id
    if target_type:
        payload["target_type"] = target_type
    if tenant_id:
        payload["tenant_id"] = tenant_id
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if details:
        payload["details"] = details

    # Using structlog, but audit entries are written as explicit single-line JSON for clarity.
    _audit_logger.info(json.dumps(payload, default=str, ensure_ascii=False))
