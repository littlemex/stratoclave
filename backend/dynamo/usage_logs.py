"""UsageLogs table.

Table design:
  PK: tenant_id
  SK: timestamp_log_id  (e.g. "2026-04-25T10:00:00Z#uuid4")
  GSI user-id-index: PK user_id, SK timestamp_log_id
  TTL: ttl (auto-deleted after 90 days)

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
        cache_read_tokens: Optional[int] = None,
        cache_write_tokens: Optional[int] = None,
        request_id: Optional[str] = None,
        cost_microusd: Optional[int] = None,
        requested_model_id: Optional[str] = None,
        measured_bound_microusd: Optional[int] = None,
        fallback_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Insert a UsageLog record.

        When the request was priced (a dollar pool was in play), `cost_microusd`
        is persisted so the pool's `pool_settled` counter can be independently
        re-derived from the audit log — i.e. spend is auditable, not just
        asserted. Legacy callers that omit it write no cost field.

        `measured_bound_microusd` (docs/design/hard-ceiling.md, coordinator's
        ITEM 2) is the hard-ceiling reservation bound this request was priced
        at by `mvp.reservation_bound`, carried here rather than into the
        credit ledger so a tenant with no dollar pool (or one with a pool,
        for a cheap cross-check) still gets the bound recorded WITHOUT any
        shared-item write: this row is already per-request and append-only,
        so writing this attribute costs nothing and touches nothing another
        concurrent request also touches — unlike a ledger entry, which would
        need a pool row (or a synthesised one) to attach to. Absent when the
        bound was never computed for this request (the `accounting` state).
        Alongside `cost_microusd` (the ACTUAL settled charge, when priced),
        the pair on one row is exactly what a shadow-run ratio analysis
        needs — a usage-log aggregation instead of a ledger query.

        `model_id` is the EFFECTIVE model the request was served by (after any
        P0-11 cascade). `requested_model_id` (P0-11 visibility) is the
        client-requested model, canonicalized by the caller; absent on legacy
        rows, so readers MUST treat a missing value as "unknown", never as
        "no fallback". The fallback BOOL is derived at read from the two ids —
        it is deliberately not persisted (no second source of truth to backfill
        or let go stale vs the ids).

        `fallback_reason` (F3, contract R38) is a DIFFERENT fact from the
        derived bool: not "did it fall back" but "why". That WHY is not
        derivable from the two ids after the fact -- it is a fact about the
        router's decision at reserve time -- so it is captured here, at
        write time, one additive attribute on this per-request, append-only
        row (the same argument `measured_bound_microusd` already rests on:
        this write touches nothing another concurrent request also writes).
        Absent on every row this deployment has produced before this field
        existed, and on any row where no fallback occurred.
        """
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
        # The cache legs are written only when the provider reported them, and are
        # recorded because this row's stated purpose is that spend be re-derivable from
        # it: a charge that included cached tokens cannot be re-derived from input and
        # output alone. Absent means "not reported", which is a different fact from zero
        # — the difference between a model that does not cache and a request that did
        # not hit the cache.
        if cache_read_tokens is not None:
            item["cache_read_tokens"] = Decimal(int(cache_read_tokens))
        if cache_write_tokens is not None:
            item["cache_write_tokens"] = Decimal(int(cache_write_tokens))
        if cost_microusd is not None:
            item["cost_microusd"] = Decimal(int(cost_microusd))
        if requested_model_id is not None:
            item["requested_model_id"] = requested_model_id
        if measured_bound_microusd is not None:
            item["measured_bound_microusd"] = Decimal(int(measured_bound_microusd))
        if fallback_reason is not None:
            item["fallback_reason"] = fallback_reason
        self._table.put_item(Item=item)
        return item
