"""Repository for the Users table."""
import time
from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, users_table_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> int:
    return int(time.time())


class UsersRepository:
    """CRUD operations for the Users table.

    Table design:
      PK: user_id (Cognito sub)
      SK: sk      (fixed value such as "PROFILE")
      GSI email-index: PK email
      GSI auth-provider-user-id-index: PK auth_provider_user_id

    Optional attributes added in Phase S:
      auth_method: "cognito" | "sso"
      sso_account_id: str
      sso_principal_arn: str
      last_sso_login_at: str (ISO 8601)
    """

    SK_PROFILE = "PROFILE"

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(table_name or users_table_name())

    def put_user(
        self,
        *,
        user_id: str,
        email: str,
        auth_provider: str,
        auth_provider_user_id: str,
        org_id: str,
        roles: Optional[list[str]] = None,
        auth_method: Optional[str] = None,
        sso_account_id: Optional[str] = None,
        sso_principal_arn: Optional[str] = None,
        locale: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new user or update an existing one.

        Preserves created_at from the existing item if present; otherwise uses the current time.
        Also handles Phase S SSO attributes.

        ``locale`` (i18n): UI display language. If explicitly provided it overwrites the
        stored value; otherwise the existing value is preserved; if neither exists the
        default ``"ja"`` is used. The backend restricts accepted values to ``{"en", "ja"}``
        (validation is the caller's responsibility).
        """
        now = _now_iso()
        existing = self.get_by_user_id(user_id)
        created_at = (existing.get("created_at") if existing else None) or now

        item: dict[str, Any] = {
            "user_id": user_id,
            "sk": self.SK_PROFILE,
            "email": email,
            "auth_provider": auth_provider,
            "auth_provider_user_id": auth_provider_user_id,
            "org_id": org_id,
            "roles": roles or ["user"],
            "created_at": created_at,
            "updated_at": now,
        }
        # i18n: explicit > existing > default ("ja").
        if locale is not None:
            item["locale"] = locale
        elif existing and existing.get("locale"):
            item["locale"] = existing["locale"]
        else:
            item["locale"] = "ja"
        # Phase S: SSO attributes — overwrite only when explicitly provided.
        if auth_method is not None:
            item["auth_method"] = auth_method
        elif existing and existing.get("auth_method"):
            item["auth_method"] = existing["auth_method"]
        else:
            item["auth_method"] = "cognito"  # default backfill

        if sso_account_id is not None:
            item["sso_account_id"] = sso_account_id
        elif existing and existing.get("sso_account_id"):
            item["sso_account_id"] = existing["sso_account_id"]

        if sso_principal_arn is not None:
            item["sso_principal_arn"] = sso_principal_arn
        elif existing and existing.get("sso_principal_arn"):
            item["sso_principal_arn"] = existing["sso_principal_arn"]

        if existing and existing.get("last_sso_login_at"):
            item["last_sso_login_at"] = existing["last_sso_login_at"]

        self._table.put_item(Item=item)
        return item

    def record_sso_login(
        self, *, user_id: str, sso_account_id: str, sso_principal_arn: str
    ) -> None:
        """Update last_sso_login_at upon a successful SSO login."""
        self._table.update_item(
            Key={"user_id": user_id, "sk": self.SK_PROFILE},
            UpdateExpression=(
                "SET last_sso_login_at = :now, sso_account_id = :acc, "
                "sso_principal_arn = :arn, updated_at = :now"
            ),
            ExpressionAttributeValues={
                ":now": _now_iso(),
                ":acc": sso_account_id,
                ":arn": sso_principal_arn,
            },
        )

    def mark_deleted(self, user_id: str) -> Optional[dict[str, Any]]:
        """Soft-delete flow (X-1, 2026-04 critical-sweep follow-up).

        Physical deletion of the Users row used to play badly with
        Cognito's "access_token outlives the user" guarantee:

          1. Admin calls DELETE /api/mvp/admin/users/{victim}.
          2. Cognito user is deleted; Users row is deleted; UserTenants
             rows are archived.
          3. The victim still holds a JWT that is structurally valid
             (signed by Cognito pre-deletion, within `exp`).
          4. The deps.py backfill path reads `user_record = None` and
             happily re-creates the row as a fresh `user` under
             `default-org`.
          5. The victim is effectively resurrected for up to one hour.

        This method writes a *tombstone* row instead:

          * ``status="deleted"`` so the auth layer (`is_user_deleted`)
            can refuse any token that lands on it,
          * ``deleted_at``: ISO8601 audit timestamp,
          * ``token_revoked_after``: current epoch seconds so the
            existing iat-based check also fails any older JWT.

        Idempotent; callable on an already-deleted row, strictly
        advancing the watermark. Returns ``None`` when no row exists.
        """
        try:
            resp = self._table.update_item(
                Key={"user_id": user_id, "sk": self.SK_PROFILE},
                UpdateExpression=(
                    "SET #s = :deleted, deleted_at = :now_iso, "
                    "token_revoked_after = :tra, updated_at = :now_iso"
                ),
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":deleted": "deleted",
                    ":now_iso": _now_iso(),
                    ":tra": _now_epoch(),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return resp.get("Attributes")

    def revoke_all_sessions(self, user_id: str) -> Optional[dict[str, Any]]:
        """Set ``token_revoked_after`` to the current epoch second so
        every JWT issued earlier is rejected by the auth path (C-C).

        Used by tenant reassignment, role demotion, user deletion,
        and any future "force logout" operation. Returns the new
        attributes (``ReturnValues=ALL_NEW``) or ``None`` when the
        user does not exist. Cognito's own ``AdminUserGlobalSignOut``
        only kills refresh tokens, so without this watermark a stale
        access_token stays live for up to 1 h with the new org_id /
        roles shown by the Users row.
        """
        try:
            resp = self._table.update_item(
                Key={"user_id": user_id, "sk": self.SK_PROFILE},
                UpdateExpression="SET token_revoked_after = :tra, updated_at = :now",
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeValues={
                    ":tra": _now_epoch(),
                    ":now": _now_iso(),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return resp.get("Attributes")

    def update_locale(self, user_id: str, locale: str) -> Optional[dict[str, Any]]:
        """Update the user's UI locale. Returns the new item attributes
        (``ReturnValues=ALL_NEW``) or ``None`` when the user does not
        exist.

        The caller is responsible for whitelisting ``locale`` against
        the supported set before invoking this. We keep the repository
        layer storage-agnostic and only enforce structural invariants.
        """
        try:
            resp = self._table.update_item(
                Key={"user_id": user_id, "sk": self.SK_PROFILE},
                UpdateExpression="SET #loc = :l, updated_at = :now",
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeNames={"#loc": "locale"},
                ExpressionAttributeValues={":l": locale, ":now": _now_iso()},
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return resp.get("Attributes")

    def update_roles(
        self,
        user_id: str,
        new_roles: list[str],
        *,
        expected_roles: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """Replace the user's `roles` list (the authorization source of truth).

        `Users.roles` is what `deps.py` resolves and `require_permission`
        enforces, so this is the ONLY place a user's effective role changes.
        Returns the new attributes (``ALL_NEW``) or ``None`` when the user does
        not exist OR when `expected_roles` was supplied and no longer matches
        (optimistic lock — a lost update would otherwise desync the audit log
        from the stored state). The caller must whitelist role values.

        `roles` is a DynamoDB reserved word, so it is aliased.
        """
        names = {"#r": "roles"}
        values: dict[str, Any] = {":r": list(new_roles), ":now": _now_iso()}
        condition = "attribute_exists(user_id)"
        if expected_roles is not None:
            condition += " AND #r = :expected"
            values[":expected"] = list(expected_roles)
        try:
            resp = self._table.update_item(
                Key={"user_id": user_id, "sk": self.SK_PROFILE},
                UpdateExpression="SET #r = :r, updated_at = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return resp.get("Attributes")

    def get_by_user_id(self, user_id: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"user_id": user_id, "sk": self.SK_PROFILE})
        return resp.get("Item")

    def get_by_email(self, email: str) -> Optional[dict[str, Any]]:
        resp = self._table.query(
            IndexName="email-index",
            KeyConditionExpression=Key("email").eq(email),
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    def scan_admins(self, limit: int = 10) -> list[dict[str, Any]]:
        """Scan the Users table and return up to `limit` users that hold the admin role.

        Used for bootstrap-admin zero-state detection. Scans are expensive, but this
        method is called only once at lifespan startup and exits early via `limit`.

        Note: DynamoDB scans are eventually consistent. A concurrent admin creation
        during lifespan startup could be missed, but this is acceptable for the
        intended use case.

        Implementation note: `roles` is a reserved word in DynamoDB and cannot be
        referenced directly in ProjectionExpression — it is aliased via ExpressionAttributeNames.
        """
        from boto3.dynamodb.conditions import Attr

        resp = self._table.scan(
            FilterExpression=Attr("sk").eq(self.SK_PROFILE)
            & Attr("roles").contains("admin"),
            Limit=max(limit, 1),
            ProjectionExpression="user_id, email, #r",
            ExpressionAttributeNames={"#r": "roles"},
        )
        return resp.get("Items", [])
