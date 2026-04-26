"""Users テーブルのリポジトリ."""
from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key

from .client import get_dynamodb_resource, users_table_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsersRepository:
    """Users テーブルへの CRUD.

    テーブル設計:
      PK: user_id (Cognito sub)
      SK: sk      ("PROFILE" など固定)
      GSI email-index: PK email
      GSI auth-provider-user-id-index: PK auth_provider_user_id

    Phase S で追加のオプショナル属性:
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
    ) -> dict[str, Any]:
        """新規ユーザー作成 or 既存更新.

        created_at は既存 item があれば保持、無ければ今時刻. Phase S 追加属性も扱う.
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
        # Phase S: SSO 系属性 (明示指定時のみ上書き)
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
        """SSO login 成功時、last_sso_login_at を更新."""
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
