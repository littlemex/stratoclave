"""UsageLogs テーブル.

テーブル設計:
  PK: tenant_id
  SK: timestamp_log_id  (例: "2026-04-25T10:00:00Z#uuid4")
  GSI user-id-index: PK user_id, SK timestamp_log_id
  TTL: ttl (90 日後自動削除)

PII handling (A-19-pii):
  Caller emails are *not* persisted in plaintext. ``record()`` accepts
  ``user_email`` for backwards-compatible call sites but stores it as
  ``user_email_hash = "pii:" + sha256(email_lower)``. Filtering by
  email therefore needs to hash the lookup value the same way; UI
  displays should resolve ``user_id → email`` against the Users table
  on demand instead of reading from the audit row.
"""
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from .client import get_dynamodb_resource, usage_logs_table_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_epoch(days: int = 90) -> int:
    from datetime import timedelta

    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def hash_user_email(email: str) -> str:
    """Return the deterministic, prefixed hash used in the audit log.

    Lower-cased before hashing so case differences in caller-supplied
    emails (Cognito normalises but external IdPs may not) collapse to
    the same audit row.
    """
    h = hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()
    return f"pii:{h}"


class UsageLogsRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or usage_logs_table_name()
        )

    def record(
        self,
        *,
        tenant_id: str,
        user_id: str,
        user_email: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """UsageLog レコードを挿入."""
        now = _now_iso()
        log_id = request_id or str(uuid4())
        # A-19-pii: never persist the email in plaintext. Hash with a
        # ``pii:`` prefix so legacy readers explicitly see they are
        # dealing with a one-way hash, not a lookup field.
        email_hash = hash_user_email(user_email) if user_email else None
        item: dict[str, Any] = {
            "tenant_id": tenant_id,
            "timestamp_log_id": f"{now}#{log_id}",
            "user_id": user_id,
            "user_email_hash": email_hash,
            "model_id": model_id,
            "input_tokens": Decimal(input_tokens),
            "output_tokens": Decimal(output_tokens),
            "total_tokens": Decimal(input_tokens + output_tokens),
            "recorded_at": now,
            "ttl": _ttl_epoch(),
        }
        self._table.put_item(Item=item)
        return item
