"""Admin API: User creation (Phase 2 v2.1).

POST /api/mvp/admin/users
    Input: {
      "email": "user@example.com",
      "role": "user" | "team_lead" | "admin"  (optional, default "user"),
      "tenant_id": "tenant-xxx"  (optional, default DEFAULT_ORG_ID),
      "total_credit": int  (optional, overrides Tenant.default_credit)
    }
    Steps:
      1. Require `users:create` permission.
      2. For role=="admin", check the ALLOW_ADMIN_CREATION=true env gate (Critical C-D).
      3. Cognito AdminCreateUser (auto-generate temporary password, SUPPRESS email delivery).
      4. Create DynamoDB Users + UserTenants records for the specified tenant.
         - If total_credit is unset, fall back to Tenant.default_credit, then DEFAULT_TENANT_CREDIT.
      5. Emit audit log (event=admin_created when creating an admin user).
    Response:
      { "email", "user_id", "temporary_password", "user_pool_id", "org_id", "role" }
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal, Optional

import boto3
from boto3.dynamodb.conditions import Key as boto3_key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
)
from dynamo.client import get_dynamodb_resource

from .authz import (
    admin_creation_allowed,
    log_audit_event,
    require_permission,
    warn_if_admin_creation_enabled_in_production,
)
from .cognito_admin import delete_user as cognito_delete_user, global_sign_out, update_org_id
from .deps import DEFAULT_ORG_ID, AuthenticatedUser
from .me import SUPPORTED_LOCALES, Locale


router = APIRouter(prefix="/api/mvp/admin", tags=["mvp-admin"])
_log = logging.getLogger(__name__)


Role = Literal["admin", "team_lead", "user"]


class CreateUserRequest(BaseModel):
    """Pydantic Literal + extra=forbid enforces type constraints (Critical C-D / Security 4.1)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    role: Role = "user"
    tenant_id: Optional[str] = Field(default=None, max_length=64)
    total_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    # i18n: admin can pre-set the new user's UI locale. Omit = server
    # default ("ja"). The new user can change it later via PATCH /me.
    locale: Optional[Locale] = None


class CreateUserResponse(BaseModel):
    email: str
    user_id: str
    # P0-3: temporary_password is omitted from responses by default to prevent
    # leakage via access logs, HAR files, or browser devtools.
    # Included only when EXPOSE_TEMPORARY_PASSWORD=true is set (for compatibility).
    # Recommended migration: ForcePasswordReset + SES delivery / one-time secret link (P1).
    temporary_password: Optional[str] = None
    user_pool_id: str
    org_id: str
    role: Role


def _cognito_client():
    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("cognito-idp", region_name=region)


def _require_user_pool_id() -> str:
    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    if not pool_id:
        raise HTTPException(
            status_code=500,
            detail="COGNITO_USER_POOL_ID is not configured",
        )
    return pool_id


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(
    body: CreateUserRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:create")),
) -> CreateUserResponse:
    # Admin creation gate (Critical C-D, P1-A time-bounded in production).
    if body.role == "admin":
        if not admin_creation_allowed():
            raise HTTPException(
                status_code=403,
                detail=(
                    "admin role creation is disabled. In development set "
                    "ALLOW_ADMIN_CREATION=true. In production set "
                    "ALLOW_ADMIN_CREATION=true AND "
                    "ALLOW_ADMIN_CREATION_UNTIL=<future-epoch-seconds>; "
                    "the gate auto-closes once the epoch passes."
                ),
            )
        # Emit a per-request audit warning whenever the gate is
        # actively open in production so forgetting to close it is
        # impossible to miss in CloudWatch alerts.
        warn_if_admin_creation_enabled_in_production(_log)

    pool_id = _require_user_pool_id()
    email = body.email.lower().strip()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="invalid email format")

    # Resolve tenant: if specified, verify it exists; otherwise fall back to default-org.
    tenant_id = body.tenant_id or DEFAULT_ORG_ID
    tenants_repo = TenantsRepository()
    tenant_rec = tenants_repo.get(tenant_id)
    if not tenant_rec:
        raise HTTPException(status_code=422, detail=f"tenant_id not found: {tenant_id}")

    cognito = _cognito_client()

    # Cognito AdminCreateUser
    try:
        resp = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:org_id", "Value": tenant_id},
            ],
            DesiredDeliveryMediums=[],
            MessageAction="SUPPRESS",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UsernameExistsException":
            raise HTTPException(status_code=409, detail="User already exists")
        raise HTTPException(status_code=502, detail=f"Cognito error: {code}")

    cognito_user = resp["User"]
    sub: Optional[str] = None
    for attr in cognito_user.get("Attributes", []):
        if attr.get("Name") == "sub":
            sub = attr.get("Value")
            break
    if not sub:
        raise HTTPException(status_code=502, detail="Cognito response missing sub")

    # Issue an explicit temporary password (Permanent=False triggers NEW_PASSWORD_REQUIRED).
    temp_password = _generate_temp_password()
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=email,
            Password=temp_password,
            Permanent=False,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise HTTPException(status_code=502, detail=f"Cognito set_password error: {code}")

    # DynamoDB: Users
    users_repo = UsersRepository()
    user_tenants_repo = UserTenantsRepository()

    users_repo.put_user(
        user_id=sub,
        email=email,
        auth_provider="cognito",
        auth_provider_user_id=sub,
        org_id=tenant_id,
        roles=[body.role],
        locale=body.locale,
    )

    # DynamoDB: UserTenants (if total_credit is None, ensure() falls back to
    # the tenant default). Explicit admin re-add is the one path that is
    # allowed to resurrect an archived membership — `/api/mvp/me` must not
    # (P0-1, see dynamo/user_tenants.py).
    user_tenants_repo.ensure(
        user_id=sub,
        tenant_id=tenant_id,
        role=body.role,
        total_credit=body.total_credit,
        allow_resurrection=True,
    )

    # Always emit an audit log; use event=admin_created when the new user is an admin.
    log_audit_event(
        event="admin_created" if body.role == "admin" else "user_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=sub,
        target_type="user",
        tenant_id=tenant_id,
        details={"email": email, "role": body.role, "allow_admin_creation": admin_creation_allowed()},
    )

    # Expose temporary_password in the response only when EXPOSE_TEMPORARY_PASSWORD=true.
    # By default it is None to prevent leakage via access logs, HAR files, or browser devtools.
    # In the default case, admins should use the Cognito console to send a password-reset email
    # or retrieve the password by running bootstrap-admin.sh manually.
    expose_temp_password = (
        os.getenv("EXPOSE_TEMPORARY_PASSWORD", "false").lower() == "true"
    )
    return CreateUserResponse(
        email=email,
        user_id=sub,
        temporary_password=temp_password if expose_temp_password else None,
        user_pool_id=pool_id,
        org_id=tenant_id,
        role=body.role,
    )


def _generate_temp_password(length: int = 16) -> str:
    """Generate a random password satisfying Cognito's policy (at least one upper, lower, digit, and symbol)."""
    import secrets
    import string

    alphabet_lower = string.ascii_lowercase
    alphabet_upper = string.ascii_uppercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+[]{}"

    required = [
        secrets.choice(alphabet_lower),
        secrets.choice(alphabet_upper),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    remain_pool = alphabet_lower + alphabet_upper + digits + symbols
    remain = [secrets.choice(remain_pool) for _ in range(max(length - len(required), 4))]
    chars = required + remain
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# -----------------------------------------------------------------------
# Listing / inspection APIs
# -----------------------------------------------------------------------
class UserSummary(BaseModel):
    user_id: str
    email: str
    roles: list[str]
    org_id: str
    total_credit: int = 0
    credit_used: int = 0
    remaining_credit: int = 0
    created_at: Optional[str] = None
    # Phase S: SSO / auth method metadata
    auth_method: Optional[str] = None
    sso_account_id: Optional[str] = None
    sso_principal_arn: Optional[str] = None
    last_sso_login_at: Optional[str] = None
    # i18n: current UI locale for this user (server-clamped to supported set).
    locale: Optional[Locale] = None


class UsersListResponse(BaseModel):
    users: list[UserSummary]
    next_cursor: Optional[str] = None


def _encode_cursor(last_key: Optional[dict]) -> Optional[str]:
    if not last_key:
        return None
    import base64
    import json
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    import base64
    import json
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _enrich_user_with_credit(user_item: dict, repo: UserTenantsRepository) -> UserSummary:
    """Build a UserSummary by attaching active UserTenants credit data to a Users item."""
    org_id = str(user_item.get("org_id") or DEFAULT_ORG_ID)
    summary = repo.credit_summary(str(user_item["user_id"]), org_id)
    roles_raw = user_item.get("roles") or []
    if isinstance(roles_raw, str):
        roles = [roles_raw]
    else:
        roles = [str(r) for r in roles_raw]
    raw_locale = user_item.get("locale")
    locale: Optional[Locale] = raw_locale if raw_locale in SUPPORTED_LOCALES else None  # type: ignore[assignment]
    return UserSummary(
        user_id=str(user_item["user_id"]),
        email=str(user_item.get("email") or ""),
        roles=roles,
        org_id=org_id,
        total_credit=summary["total_credit"],
        credit_used=summary["credit_used"],
        remaining_credit=summary["remaining_credit"],
        created_at=user_item.get("created_at"),
        auth_method=user_item.get("auth_method"),
        sso_account_id=user_item.get("sso_account_id"),
        sso_principal_arn=user_item.get("sso_principal_arn"),
        last_sso_login_at=user_item.get("last_sso_login_at"),
        locale=locale,
    )


@router.get("/users", response_model=UsersListResponse)
def list_users(
    cursor: Optional[str] = None,
    limit: int = 50,
    role: Optional[Role] = None,
    tenant_id: Optional[str] = None,
    _admin: AuthenticatedUser = Depends(require_permission("users:read")),
) -> UsersListResponse:
    """List all users (Scan + cursor pagination).

    A full Scan is sufficient at MVP scale. role/tenant filtering is applied server-side.
    limit is clamped to a maximum of 100 (H2).
    """
    users_repo = UsersRepository()
    user_tenants_repo = UserTenantsRepository()
    limit = max(1, min(limit, 100))

    scan_kwargs: dict[str, Any] = {"Limit": limit}
    decoded = _decode_cursor(cursor)
    if decoded:
        scan_kwargs["ExclusiveStartKey"] = decoded

    resp = users_repo._table.scan(**scan_kwargs)  # Single-table Scan; PROFILE SK filter applied below.
    raw_items = resp.get("Items", [])
    next_cursor = _encode_cursor(resp.get("LastEvaluatedKey"))

    results: list[UserSummary] = []
    for item in raw_items:
        if item.get("sk") != users_repo.SK_PROFILE:
            continue
        roles_raw = item.get("roles") or []
        roles_list = [roles_raw] if isinstance(roles_raw, str) else [str(r) for r in roles_raw]
        if role and role not in roles_list:
            continue
        if tenant_id and str(item.get("org_id") or "") != tenant_id:
            continue
        results.append(_enrich_user_with_credit(item, user_tenants_repo))

    return UsersListResponse(users=results, next_cursor=next_cursor)


@router.get("/users/{user_id}", response_model=UserSummary)
def get_user(
    user_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("users:read")),
) -> UserSummary:
    users_repo = UsersRepository()
    item = users_repo.get_by_user_id(user_id)
    if not item:
        raise HTTPException(status_code=404, detail="User not found")
    return _enrich_user_with_credit(item, UserTenantsRepository())


# -----------------------------------------------------------------------
# Delete (with last-admin protection)
# -----------------------------------------------------------------------
def _count_active_admins() -> int:
    """Count active users whose `roles` include admin in DynamoDB Users.

    A-03-admin: soft-delete tombstones (status="deleted") created by ``mark_deleted``
    are not counted as active. Without this check, the delete API would incorrectly
    permit the deletion of the last admin immediately after the tombstone is written,
    leaving zero active admins.
    """
    repo = UsersRepository()
    resp = repo._table.scan()
    count = 0
    for item in resp.get("Items", []):
        if item.get("sk") != repo.SK_PROFILE:
            continue
        if item.get("status") == "deleted":
            continue
        roles = item.get("roles") or []
        if "admin" in roles:
            count += 1
    return count


@router.delete("/users/{user_id}")
def delete_user_endpoint(
    user_id: str,
    actor: AuthenticatedUser = Depends(require_permission("users:delete")),
) -> Response:
    """Delete a user from Cognito and DynamoDB.

    Last-admin protection: returns 409 if the target holds the admin role and is the only active admin.
    UsageLogs are retained for audit purposes.
    """
    users_repo = UsersRepository()
    item = users_repo.get_by_user_id(user_id)
    if not item:
        raise HTTPException(status_code=404, detail="User not found")

    email = str(item.get("email") or "")
    roles = item.get("roles") or []
    roles_list = [roles] if isinstance(roles, str) else list(roles)

    if "admin" in roles_list and _count_active_admins() <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last admin user")

    # Prevent self-deletion to avoid accidental lockout.
    if user_id == actor.user_id:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")

    # Delete from Cognito first; abort if this fails.
    if email:
        cognito_delete_user(email)
    # X-1 (2026-04 critical-sweep follow-up): Cognito's admin_delete_user
    # does NOT invalidate already-issued access_tokens. We previously
    # tried to delete the Users row outright, but that deleted the
    # `token_revoked_after` watermark alongside it, so a backfill on
    # the next request would happily rebuild the row as a fresh `user`
    # and resurrect the victim for up to an hour. Call global_sign_out
    # so Cognito kills the refresh token AND replace the Users row
    # with a soft-delete tombstone that `deps.is_user_deleted` blocks
    # before any backfill can run.
    try:
        global_sign_out(user_id)
    except Exception as e:  # pragma: no cover — Cognito hiccup
        _log.warning("global_sign_out_failed_on_delete", extra={"user_id": user_id, "error": str(e)})

    # Soft-delete the Users row instead of a physical delete (X-1).
    users_repo.mark_deleted(user_id)

    # Z-1 (2026-04 third blind review): sk-stratoclave-* API keys live
    # in a separate table and survive mark_deleted + global_sign_out.
    # The deps.py watermark check catches them at auth time (defence
    # in depth) but we also sweep the rows here so admin listings
    # reflect the cut-off immediately and restore operations do not
    # silently re-enable them.
    try:
        from dynamo import ApiKeysRepository as _ApiKeysRepository

        revoked_count = _ApiKeysRepository().revoke_all_for_user(
            user_id, actor_user_id=actor.user_id
        )
        if revoked_count:
            log_audit_event(
                event="api_keys_revoked_on_user_delete",
                actor_id=actor.user_id,
                actor_email=actor.email,
                target_id=user_id,
                target_type="user",
                details={"count": revoked_count},
            )
    except Exception as e:  # pragma: no cover — repo-level boto error
        _log.warning(
            "api_keys_revoke_failed_on_delete",
            extra={"user_id": user_id, "error": str(e)},
        )

    # Archive UserTenants rows (preserve history).
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    user_tenants_repo = UserTenantsRepository()
    resp = user_tenants_repo._table.query(
        KeyConditionExpression=boto3_key("user_id").eq(user_id),
    )
    for ut in resp.get("Items", []):
        user_tenants_repo._table.update_item(
            Key={"user_id": user_id, "tenant_id": ut["tenant_id"]},
            UpdateExpression="SET #s = :archived, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":archived": "archived", ":now": now_iso},
        )

    log_audit_event(
        event="user_deleted",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        details={"email": email, "roles": roles_list},
    )
    return Response(status_code=204)


# -----------------------------------------------------------------------
# Admin profile patch (scope-limited)
# -----------------------------------------------------------------------
class AdminUpdateUserRequest(BaseModel):
    """Admin-side scoped update of a target user's mutable profile fields.

    **Scope is deliberately limited to** ``locale`` in this PR. We did not
    widen to ``role`` or ``email`` here because admin-level role changes
    are a privilege-escalation surface and need their own audit +
    transactional handling (Cognito group sync, last-admin protection,
    CloudWatch alerting). A future change can add a sibling endpoint
    such as ``PATCH /users/{user_id}/role`` if that ever lands.
    """

    model_config = ConfigDict(extra="forbid")
    locale: Locale = Field(..., description="UI locale to set for the target user")


@router.patch("/users/{user_id}", response_model=UserSummary)
def admin_update_user(
    user_id: str,
    body: AdminUpdateUserRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:update")),
) -> UserSummary:
    """Admin sets a target user's UI locale.

    - `users:update` permission required (same as credit overwrite).
    - Returns `UserSummary` with the refreshed `locale` so the UI can
      redraw the row without a second round-trip.
    - Audit-logged (`event=user_locale_updated_by_admin`) with target
      and new locale.
    """
    users_repo = UsersRepository()
    existing = users_repo.get_by_user_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    attrs = users_repo.update_locale(user_id, body.locale)
    if attrs is None:
        raise HTTPException(status_code=404, detail="User not found")

    log_audit_event(
        event="user_locale_updated_by_admin",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        before={"locale": existing.get("locale")},
        after={"locale": body.locale},
    )

    refreshed = users_repo.get_by_user_id(user_id) or existing
    return _enrich_user_with_credit(refreshed, UserTenantsRepository())


# -----------------------------------------------------------------------
# Role change (the SINGLE role-mutation chokepoint)
# -----------------------------------------------------------------------
def _set_user_role(
    *,
    user_id: str,
    new_role: str,
    actor: AuthenticatedUser,
) -> dict[str, Any]:
    """The one place a user's authorization role changes.

    `Users.roles` is the sole source of truth resolved by deps.py, so this is
    the ONLY correct way to promote/demote. It:

    - replaces `Users.roles` with exactly `[new_role]` (single-role model),
    - refuses to remove the last admin (last-admin protection), on EVERY path
      that changes a role — not just delete,
    - refuses to strip team_lead from a user who still owns a tenant (409:
      transfer ownership first — no silent owner-less tenants),
    - is idempotent (same role → no-op, but still audited),
    - signs the user out + stamps the session watermark so a cached JWT cannot
      keep acting with the old role (deps resolves roles live, so enforcement is
      immediate; this is defence-in-depth for any future claim cache). API keys
      read `Users.roles` live per request, so no key sweep is needed.

    Returns the refreshed Users row. Raises HTTPException on any guard.
    """
    users_repo = UsersRepository()
    existing = users_repo.get_by_user_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    roles_raw = existing.get("roles") or []
    old_roles = [roles_raw] if isinstance(roles_raw, str) else [str(r) for r in roles_raw]
    was_admin = "admin" in old_roles

    # Idempotent no-op (still audited below so the intent is recorded).
    if old_roles == [new_role]:
        log_audit_event(
            event="user_role_unchanged",
            actor_id=actor.user_id,
            actor_email=actor.email,
            target_id=user_id,
            target_type="user",
            before={"roles": old_roles},
            after={"roles": [new_role]},
        )
        return existing

    # Last-admin protection: a demotion (admin -> non-admin) must not drop the
    # active admin count to zero. NOTE: _count_active_admins() is a scan, so
    # there is a TOCTOU window under concurrent demotions — accepted and made
    # visible here (a stricter atomic counter is a follow-up). The optimistic
    # lock below still prevents THIS row being lost-updated.
    if was_admin and new_role != "admin" and _count_active_admins() <= 1:
        raise HTTPException(status_code=409, detail="Cannot demote the last admin user")

    # Demoting out of team_lead: refuse while the user still owns any tenant, so
    # a tenant is never left owner-less by a silent pointer clear.
    if "team_lead" in old_roles and new_role != "team_lead":
        owned = _tenants_owned_by(user_id)
        if owned:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Transfer tenant ownership before demoting this team_lead "
                    f"(still owns: {', '.join(owned)})"
                ),
            )

    updated = users_repo.update_roles(user_id, [new_role], expected_roles=old_roles)
    if updated is None:
        # Either the row vanished or a concurrent role change beat us (optimistic
        # lock). Surface a conflict so the caller re-reads rather than silently
        # racing the audit log against stored state.
        raise HTTPException(status_code=409, detail="Role changed concurrently; retry")

    log_audit_event(
        event="user_role_changed",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        before={"roles": old_roles},
        after={"roles": [new_role]},
    )

    # Defence-in-depth immediate revocation of stale JWTs (deps enforces the new
    # role live regardless).
    try:
        global_sign_out(user_id)
    except Exception as e:  # pragma: no cover — Cognito hiccup
        _log.warning("global_sign_out_failed_on_role_change", extra={"user_id": user_id, "error": str(e)})
    users_repo.revoke_all_sessions(user_id)

    return users_repo.get_by_user_id(user_id) or updated


def _tenants_owned_by(user_id: str) -> list[str]:
    """Active tenant ids owned by `user_id` (team_lead_user_id pointer).

    Uses the team-lead-index (not a full scan) and excludes archived tenants.
    """
    return [
        str(t.get("tenant_id"))
        for t in TenantsRepository().list_by_owner(user_id)
    ]


class AdminSetRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role = Field(..., description="The single role to assign (replaces current roles)")


@router.patch("/users/{user_id}/role", response_model=UserSummary)
def admin_set_user_role(
    user_id: str,
    body: AdminSetRoleRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:update")),
) -> UserSummary:
    """Promote/demote a user by replacing their role (the authorization SoT).

    Guards (see `_set_user_role`): last-admin protection, team_lead-owns-tenant
    block, optimistic lock, audit, immediate sign-out. Role changes are refused
    for API-key auth so a key can never escalate its own owner (a key that holds
    `users:update` is still blocked here)."""
    if actor.auth_kind == "api_key":
        # A bearer key must NEVER be able to change roles — that is a direct
        # path to escalating its own owner to admin. Human (JWT) actor only.
        raise HTTPException(status_code=403, detail="Role changes require an interactive session, not an API key")
    refreshed = _set_user_role(user_id=user_id, new_role=body.role, actor=actor)
    return _enrich_user_with_credit(refreshed, UserTenantsRepository())


# -----------------------------------------------------------------------
# Tenant assignment (switch)
# -----------------------------------------------------------------------
class AssignTenantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)
    total_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    new_role: Role = "user"


@router.put("/users/{user_id}/tenant", response_model=UserSummary)
def assign_tenant(
    user_id: str,
    body: AssignTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:assign-tenant")),
) -> UserSummary:
    """Switch a user to the specified tenant (TransactWriteItems + Cognito saga)."""
    users_repo = UsersRepository()
    user_item = users_repo.get_by_user_id(user_id)
    if not user_item:
        raise HTTPException(status_code=404, detail="User not found")

    old_tenant_id = str(user_item.get("org_id") or DEFAULT_ORG_ID)
    new_tenant_id = body.tenant_id.strip()

    if old_tenant_id == new_tenant_id:
        raise HTTPException(status_code=409, detail="User is already assigned to this tenant")

    tenants_repo = TenantsRepository()
    new_tenant = tenants_repo.get(new_tenant_id)
    if not new_tenant:
        raise HTTPException(status_code=422, detail=f"tenant_id not found: {new_tenant_id}")

    user_tenants_repo = UserTenantsRepository()

    # (1) DynamoDB TransactWriteItems
    try:
        user_tenants_repo.switch_tenant(
            user_id=user_id,
            old_tenant_id=old_tenant_id,
            new_tenant_id=new_tenant_id,
            new_role=body.new_role,
            new_total_credit=body.total_credit,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=f"Tenant switch conflict: {e}")

    # (1b) Apply the role to Users.roles — the authorization source of truth.
    # BUG FIX (capability audit): switch_tenant writes new_role only into the
    # per-tenant UserTenants row and Users.org_id, NOT Users.roles, which is
    # what deps.py enforces. So `new_role` was a silent no-op for authorization
    # — a demotion left admin power intact (privilege retention). Route the role
    # change through the same last-admin-guarded update as PATCH /role.
    roles_raw = user_item.get("roles") or []
    old_roles = [roles_raw] if isinstance(roles_raw, str) else [str(r) for r in roles_raw]
    if old_roles != [body.new_role]:
        if "admin" in old_roles and body.new_role != "admin" and _count_active_admins() <= 1:
            raise HTTPException(status_code=409, detail="Cannot demote the last admin user")
        if users_repo.update_roles(user_id, [body.new_role], expected_roles=old_roles) is None:
            raise HTTPException(status_code=409, detail="Role changed concurrently; retry")

    # (2) Cognito saga: update attribute then global sign out.
    update_org_id(user_id, new_tenant_id)
    global_sign_out(user_id)
    # C-C (2026-04 critical sweep): stamp a server-side session
    # revocation watermark. Cognito's global_sign_out kills refresh
    # tokens but leaves the access_token live until its 1 h exp; a
    # stale tab would otherwise keep acting with the NEW org_id until
    # then. The watermark fails those stale JWTs at deps.py.
    users_repo.revoke_all_sessions(user_id)
    # Z-1 (2026-04 third blind review): sweep sk-stratoclave-* API
    # keys belonging to this user too. The watermark check in
    # deps.py refuses them at auth time anyway, but an explicit
    # revoke makes the admin list reflect reality right after the
    # switch and avoids "zombie rows" for compliance reviews.
    try:
        from dynamo import ApiKeysRepository as _ApiKeysRepository

        _ApiKeysRepository().revoke_all_for_user(
            user_id, actor_user_id=actor.user_id
        )
    except Exception as e:  # pragma: no cover
        _log.warning(
            "api_keys_revoke_failed_on_switch",
            extra={"user_id": user_id, "error": str(e)},
        )

    log_audit_event(
        event="user_tenant_switched",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        tenant_id=new_tenant_id,
        before={"tenant_id": old_tenant_id},
        after={"tenant_id": new_tenant_id, "role": body.new_role, "total_credit": body.total_credit},
    )

    # Re-fetch the Users row to return up-to-date data.
    fresh = users_repo.get_by_user_id(user_id) or user_item
    return _enrich_user_with_credit(fresh, user_tenants_repo)


# -----------------------------------------------------------------------
# Credit overwrite
# -----------------------------------------------------------------------
class SetCreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_credit: int = Field(ge=0, le=10_000_000)
    reset_used: bool = False


@router.patch("/users/{user_id}/credit", response_model=UserSummary)
def set_credit(
    user_id: str,
    body: SetCreditRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:update")),
) -> UserSummary:
    users_repo = UsersRepository()
    user_item = users_repo.get_by_user_id(user_id)
    if not user_item:
        raise HTTPException(status_code=404, detail="User not found")
    tenant_id = str(user_item.get("org_id") or DEFAULT_ORG_ID)

    user_tenants_repo = UserTenantsRepository()
    prev = user_tenants_repo.credit_summary(user_id, tenant_id)
    try:
        user_tenants_repo.overwrite_credit(
            user_id=user_id,
            tenant_id=tenant_id,
            total_credit=body.total_credit,
            reset_used=body.reset_used,
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"Credit update conflict: {e}")

    log_audit_event(
        event="credit_overwritten",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        tenant_id=tenant_id,
        before=prev,
        after={"total_credit": body.total_credit, "reset_used": body.reset_used},
    )

    return _enrich_user_with_credit(user_item, user_tenants_repo)
