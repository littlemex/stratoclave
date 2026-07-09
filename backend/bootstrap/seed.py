"""Idempotent seed executed at backend lifespan startup.

Enables OSS users to go from clone → deploy → admin login with zero manual
steps. On startup, the following are written to DynamoDB idempotently:

1. Permissions (3 roles: admin / team_lead / user)
   - backend/permissions.json is the source of truth
   - No-op if the existing version matches; overwrites on mismatch
2. Default Tenant (default-org)
   - Written with attribute_not_exists on the tenants table
   - Left untouched if it already exists

Invariants:
- Running twice produces the same state (idempotent)
- A broken permissions.json does not prevent backend startup (logged as warning)
- No DynamoDB write occurs when the existing permissions version matches

Environment variables:
- DEFAULT_ORG_ID: tenant_id for the default tenant (default "default-org")
- DEFAULT_TENANT_CREDIT: default_credit value (default 100000, int)
- PERMISSIONS_SEED_FILE: path to permissions.json (default backend/permissions.json)
- STRATOCLAVE_DISABLE_SEED: skip seed when set to "true" (for testing)
- STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL: email for the initial admin user (optional).
    When set and no admin-role user exists in the Users table, creates a Cognito
    admin user and registers it in the Users table (P0-1).
    Prevents 401/422 failures on bootstrap-admin.sh and avoids dead-on-arrival
    OSS forks. The temporary password is written to AWS Secrets Manager
    (not to logs) — see _stash_bootstrap_password for details.
- STRATOCLAVE_BOOTSTRAP_ADMIN_ORG_ID: tenant for the initial admin (default DEFAULT_ORG_ID)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from core.logging import get_logger
from dynamo import (
    PermissionsRepository,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
)


logger = get_logger(__name__)


# Default path for permissions.json (directly under the backend/ directory).
# This file is backend/bootstrap/seed.py, so parent.parent is backend/.
_DEFAULT_PERMISSIONS_FILE = Path(__file__).resolve().parent.parent / "permissions.json"


def _permissions_file_path() -> Path:
    override = os.getenv("PERMISSIONS_SEED_FILE")
    if override:
        return Path(override)
    return _DEFAULT_PERMISSIONS_FILE


def seed_permissions() -> dict[str, int]:
    """Seed the Permissions table from permissions.json idempotently.

    Returns the result from PermissionsRepository.seed_from_file:
      {"total": N, "changed": M, "skipped": S}

    Exceptions are suppressed by the caller (via seed_all).
    """
    path = _permissions_file_path()
    if not path.exists():
        logger.warning(
            "permissions_seed_file_missing",
            path=str(path),
            hint="Skipping permissions seed; admin/team_lead/user roles may not work",
        )
        return {"total": 0, "changed": 0, "skipped": 0}

    result = PermissionsRepository().seed_from_file(path)
    logger.info(
        "permissions_seeded",
        path=str(path),
        total=result["total"],
        changed=result["changed"],
        skipped=result["skipped"],
    )
    return result


def seed_default_tenant() -> dict[str, Any]:
    """Idempotently put the default tenant (default-org).

    Leaves the record untouched if it already exists.
    Returns: {"tenant_id": str, "created": bool, "item": dict}
    """
    tenant_id = os.getenv("DEFAULT_ORG_ID", "default-org")
    default_credit_env = os.getenv("DEFAULT_TENANT_CREDIT")
    default_credit = int(default_credit_env) if default_credit_env else None

    result = TenantsRepository().seed_default(
        tenant_id=tenant_id,
        name="Default Organization",
        default_credit=default_credit,
        created_by="system-seed",
    )
    logger.info(
        "default_tenant_seeded",
        tenant_id=result["tenant_id"],
        created=result["created"],
    )
    return result


def _stash_bootstrap_password(*, prefix: str, password: str, email: str) -> str:
    """Stash the bootstrap admin temp password in AWS Secrets Manager.

    Lands at ``<prefix>/bootstrap-admin-temp-password``. Existing value
    is overwritten (idempotent re-boot behaviour). Returns the ARN of
    the secret so the structured log can reference it without exposing
    the plaintext.

    We intentionally write only to Secrets Manager, NOT to stderr or
    stdout: the ECS awslogs driver forwards both streams to CloudWatch
    Logs, so stderr would still leak the plaintext through the log
    retention surface. The task role already has
    ``secretsmanager:PutSecretValue`` on ``<prefix>/*`` (see
    ``iac/lib/ecs-stack.ts``).
    """
    import json

    import boto3

    client = boto3.client(
        "secretsmanager",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )
    secret_name = f"{prefix}/bootstrap-admin-temp-password"
    payload = json.dumps({"email": email, "password": password})
    try:
        resp = client.put_secret_value(SecretId=secret_name, SecretString=payload)
        return str(resp.get("ARN", secret_name))
    except client.exceptions.ResourceNotFoundException:
        resp = client.create_secret(
            Name=secret_name,
            Description=(
                "Stratoclave bootstrap admin temporary password. Delete after "
                "the operator has rotated it via Cognito."
            ),
            SecretString=payload,
        )
        return str(resp.get("ARN", secret_name))


def _generate_temp_password(length: int = 20) -> str:
    """Generate a random password satisfying Cognito's policy (at least one upper, lower, digit, and symbol)."""
    import secrets
    import string

    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    required = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    pool = lower + upper + digits + symbols
    remain = [secrets.choice(pool) for _ in range(length - len(required))]
    chars = required + remain
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def seed_bootstrap_admin() -> dict[str, Any]:
    """Seed Cognito + DynamoDB when no admin exists yet (zero-state), preventing
    dead-on-arrival OSS forks.

    Behavior:
      - No-op if STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL is not set
      - No-op if at least one admin-role user already exists
      - When both conditions are met:
          1. Cognito AdminCreateUser (lower(email), custom:org_id=tenant_id)
          2. Set a random permanent password (admin should change it afterward)
          3. Put the user in the Users table with roles=[admin, user]
          4. Ensure a UserTenants row with role=admin
          5. Stash the temporary password in AWS Secrets Manager (not in logs)

    If AdminCreateUser fails (e.g. UsernameExistsException), adopts the existing
    Cognito user and reconciles only the Users/UserTenants side.
    """
    import boto3
    from botocore.exceptions import ClientError

    email_env = os.getenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    if not email_env:
        return {"skipped": True, "reason": "STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL not set"}

    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    if not pool_id:
        logger.warning(
            "bootstrap_admin_skipped",
            reason="COGNITO_USER_POOL_ID not set; cannot provision Cognito user",
        )
        return {"skipped": True, "reason": "COGNITO_USER_POOL_ID missing"}

    tenant_id = os.getenv("STRATOCLAVE_BOOTSTRAP_ADMIN_ORG_ID") or os.getenv(
        "DEFAULT_ORG_ID", "default-org"
    )

    users_repo = UsersRepository()
    # No-op if at least one admin already exists (idempotent).
    existing_admins = users_repo.scan_admins(limit=1)
    if existing_admins:
        logger.info(
            "bootstrap_admin_skipped",
            reason="at_least_one_admin_exists",
            admin_count_sample=len(existing_admins),
        )
        return {"skipped": True, "reason": "admin exists"}

    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
    cognito = boto3.client("cognito-idp", region_name=region)

    sub: str | None = None
    created_new = True
    try:
        resp = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email_env,
            UserAttributes=[
                {"Name": "email", "Value": email_env},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:org_id", "Value": tenant_id},
            ],
            DesiredDeliveryMediums=[],
            MessageAction="SUPPRESS",
        )
        for attr in resp.get("User", {}).get("Attributes", []):
            if attr.get("Name") == "sub":
                sub = attr.get("Value")
                break
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UsernameExistsException":
            created_new = False
            try:
                ex = cognito.admin_get_user(UserPoolId=pool_id, Username=email_env)
                for attr in ex.get("UserAttributes", []):
                    if attr.get("Name") == "sub":
                        sub = attr.get("Value")
                        break
            except ClientError as ee:
                logger.error(
                    "bootstrap_admin_cognito_lookup_failed",
                    error=str(ee),
                    email=email_env,
                )
                return {"skipped": True, "reason": "cognito lookup failed"}
        else:
            logger.error(
                "bootstrap_admin_cognito_create_failed",
                error=str(e),
                email=email_env,
            )
            return {"skipped": True, "reason": f"cognito: {code}"}

    if not sub:
        logger.error("bootstrap_admin_no_sub", email=email_env)
        return {"skipped": True, "reason": "no sub resolved"}

    temp_password = _generate_temp_password()
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=email_env,
            Password=temp_password,
            Permanent=True,
        )
    except ClientError as e:
        logger.error(
            "bootstrap_admin_set_password_failed",
            error=str(e),
            email=email_env,
        )
        # Even if password setting fails, write the Users row (can be reset later).
        temp_password = ""

    # Users table
    users_repo.put_user(
        user_id=sub,
        email=email_env,
        auth_provider="cognito",
        auth_provider_user_id=sub,
        org_id=tenant_id,
        roles=["admin", "user"],
    )
    # UserTenants table (active with admin role)
    UserTenantsRepository().ensure(
        user_id=sub,
        tenant_id=tenant_id,
        role="admin",
    )

    # Sweep-4 C-Critical (C-F regression, tightened after round-4 blind
    # review). History:
    #
    #   * pre-sweep-1: `logger.info(..., temporary_password=pw)` shipped
    #     the plaintext into structured logs, which on Fargate with the
    #     awslogs driver lands in CloudWatch Logs permanently — every
    #     principal with `logs:FilterLogEvents` on /ecs/...-backend
    #     could escalate to admin retroactively.
    #   * first sweep-4 revision: moved the plaintext to `sys.stderr`.
    #     That was INSUFFICIENT: the awslogs ECS driver captures stdout
    #     AND stderr from the container process, so the secret still
    #     landed in CloudWatch — only stripped of its structured
    #     `temporary_password` field name.
    #
    # Correct solution (this revision): write the plaintext to AWS
    # Secrets Manager at `${prefix}/bootstrap-admin-temp-password`
    # with a short 7-day recovery window. The backend task role
    # already has `secretsmanager:PutSecretValue` on `${prefix}/*`
    # (see iac/lib/ecs-stack.ts). Operators retrieve it once via
    #   aws secretsmanager get-secret-value --secret-id <prefix>/bootstrap-admin-temp-password
    # and then delete it — or simply rotate the password via Cognito
    # and let the secret expire. Plaintext never touches stdout/stderr
    # / CloudWatch Logs / SIEM pipelines.
    #
    # Structured logs still record the FACT of the bootstrap (email,
    # sub, tenant, created_new) so the operation is auditable. Only
    # the credential itself is moved off-log.
    if temp_password:
        secret_arn: Optional[str] = None
        try:
            secret_arn = _stash_bootstrap_password(
                prefix=os.getenv("STRATOCLAVE_PREFIX", "stratoclave"),
                password=temp_password,
                email=email_env,
            )
        except Exception as e:
            logger.error(
                "bootstrap_admin_secret_stash_failed",
                error=str(e),
                error_type=type(e).__name__,
                email=email_env,
            )

        # A-12-log: do NOT log the admin email or Cognito Pool ID in
        # plaintext on every bootstrap, even at info level — once
        # CloudWatch ingests them they are searchable forever and any
        # one-account contributor can read them. Surface the Secrets
        # Manager ARN (already a tagged AWS resource) and a generic
        # rotation hint instead; the operator who runs the rotation
        # already knows which Pool ID to target from the deploy output.
        logger.info(
            "bootstrap_admin_created",
            user_id=sub,
            tenant_id=tenant_id,
            created_new=created_new,
            # NB: `temporary_password` is intentionally NOT included in
            # this structured record. Do not re-add it — the regression
            # guard test_bootstrap_admin_password_not_logged.py will
            # fail CI if it shows up here.
            secret_arn=secret_arn,
            instruction=(
                "Temporary password stashed in Secrets Manager (see secret_arn). "
                "Retrieve once and immediately rotate via "
                "`aws cognito-idp admin-set-user-password`."
            ),
        )
    else:
        logger.warning(
            "bootstrap_admin_created_without_password",
            user_id=sub,
            tenant_id=tenant_id,
            instruction="Set password via aws cognito-idp admin-set-user-password",
        )

    return {
        "created": True,
        "email": email_env,
        "user_id": sub,
        "tenant_id": tenant_id,
        "created_new": created_new,
    }


def seed_all() -> dict[str, Any]:
    """Top-level entry point called from the backend lifespan.

    Calls each seed function in order; individual failures do not stop
    the remaining seeds (best-effort). Returns a summary dict. The caller
    (main.py lifespan) may ignore the return value — all outcomes are
    recorded via the logger.

    Skipped entirely when STRATOCLAVE_DISABLE_SEED=true.
    """
    if os.getenv("STRATOCLAVE_DISABLE_SEED", "false").lower() == "true":
        logger.info("seed_skipped", reason="STRATOCLAVE_DISABLE_SEED=true")
        return {"skipped": True}

    summary: dict[str, Any] = {}

    # 1. Permissions
    try:
        summary["permissions"] = seed_permissions()
    except Exception as exc:
        logger.error(
            "permissions_seed_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        summary["permissions"] = {"error": str(exc)}

    # 2. Default Tenant
    try:
        summary["default_tenant"] = seed_default_tenant()
    except Exception as exc:
        logger.error(
            "default_tenant_seed_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        summary["default_tenant"] = {"error": str(exc)}

    # 3. Bootstrap Admin (only when STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL is set and no admins exist)
    try:
        summary["bootstrap_admin"] = seed_bootstrap_admin()
    except Exception as exc:
        logger.error(
            "bootstrap_admin_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        summary["bootstrap_admin"] = {"error": str(exc)}

    return summary
