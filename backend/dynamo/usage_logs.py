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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from boto3.dynamodb.conditions import Attr, Key

from .client import get_dynamodb_resource, usage_logs_table_name

#: The retention policy: how many days out a row's `ttl` attribute is set to
#: at write time. NOT a query horizon -- DynamoDB's TTL sweep is asynchronous
#: (it deletes an expired item at some unspecified time after `ttl` passes,
#: not exactly at it), so a row older than this can still be read back until
#: the sweep actually runs. Named once so a reader of an
#: aggregation response's `retention_policy_days` field and the row's own TTL
#: cannot drift.
RETENTION_DAYS = 90

#: `aggregate_by_tag` bound: the most tenant-partition pages one call will
#: read before giving up and reporting `truncated=True`. Named so the bound
#: is visible to a caller deciding whether to narrow `period` or `user_id`
#: rather than retry the same call.
MAX_AGGREGATE_PAGES = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_epoch() -> int:
    """The `ttl` value a row written right now expires at.

    There is deliberately no `days` parameter: `RETENTION_DAYS` is the single
    source and is read here. A caller able to pass its own lifetime could
    desynchronise a row's actual expiry from the number the by-tag response
    reports as the retention policy.
    """
    from datetime import timedelta

    return int((datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)).timestamp())


def _next_period(period: str) -> str:
    """The 'YYYY-MM' immediately after `period`, for a half-open range bound."""
    year, month = (int(p) for p in period.split("-", 1))
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def hash_user_email(email: str) -> str:
    """Return the deterministic, prefixed hash used in the audit log.

    Lower-cased before hashing so case differences in caller-supplied
    emails (Cognito normalises but external IdPs may not) collapse to
    the same audit row.
    """
    h = hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()
    return f"pii:{h}"


@dataclass(frozen=True)
class TagAggregateRow:
    """One (user, tag) total within a `TagAggregate`.

    `absent_count` and `dropped_grammar_count` split `requests` by
    `task_tag_source`, so the `unlabelled` row can distinguish "nobody
    asserted a tag" from "somebody asserted one and the gateway dropped it"
    -- both land on `task_tag == mvp.task_tag.SENTINEL`, and without this
    split they are the same number. `dropped_grammar_count` folds BOTH
    `mvp.task_tag.Source.DROPPED_GRAMMAR` and `DROPPED_RESERVED` rows (see
    that enum's docstring for why) -- the two counts sum to `requests` for
    every row, since a row's `task_tag` is always the sentinel when its
    source is not `asserted`. A non-sentinel row is all `asserted`
    requests, so both counts are 0 there.
    """

    user_id: str
    task_tag: str
    requests: int
    absent_count: int
    dropped_grammar_count: int
    cost_microusd: int
    #: How many of `requests` carried NO `cost_microusd` attribute at all, so
    #: `cost_microusd` above is missing them.
    #:
    #: An absent attribute and a stored zero are different facts and this is the only
    #: thing that keeps them apart: the fold reads a missing cost as 0, so without
    #: this count a request the gateway could not price is indistinguishable from one
    #: that was free. Phase 5 found a whole tenant in that state -- enforced in
    #: dollars, every row reported at zero -- and the report had no way to say so.
    #:
    #: Nonzero for rows written before the settle path priced unpooled requests, and
    #: for any request admitted with no frozen rate and no pool. Never backfilled: the
    #: rate a past reservation froze is not recoverable, and charging old usage at
    #: today's rate is exactly what freezing exists to prevent.
    requests_without_cost: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class TagAggregate:
    """Result of `UsageLogsRepository.aggregate_by_tag`."""

    rows: tuple[TagAggregateRow, ...]
    truncated: bool          # True when `max_pages` was reached before LastEvaluatedKey ran out
    pages_read: int
    # Rows with no tag attributes at all, COUNTED, never folded into a row:
    # those requests never asserted a tag, and grouping them under the
    # unasserted-tag sentinel would claim a labelling fact the row does not
    # carry (see `UsageLogsRepository.record`).
    legacy_rows: int
    # Rows with EXACTLY ONE of the two tag attributes.
    # `record` now refuses to write one of these (a half-pair raises), so a
    # row here did not come from a well-behaved caller of `record` -- it is
    # corruption (a migration, a manual edit, a pre-check-era row) to
    # surface, not history to fold silently into either `rows` or
    # `legacy_rows`.
    malformed_rows: int


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
        task_tag: Optional[str] = None,
        task_tag_source: Optional[str] = None,
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

        `task_tag` / `task_tag_source` (see `mvp.task_tag`) are the resolved
        caller-asserted tag and how it was resolved, carried in from the
        request's edge context rather than re-derived here. They are written
        ONLY when BOTH are supplied, and never defaulted at write time: a row
        carrying neither is one written before this pair of attributes
        existed, and a reader MUST report that as "unknown" -- never as the
        unasserted-tag sentinel, which would claim a labelling fact this row
        never recorded. The same "absent is a legacy fact" reading
        `requested_model_id` already rests on.

        A half-pair (exactly one of the two supplied) is rejected: this
        raises `ValueError` rather than silently writing neither, because a
        caller that has one leg but not the other has a bug worth surfacing
        at the call site, not a legacy row worth absorbing. The two keyword
        arguments stay independent -- a caller CAN still write this call --
        so the guarantee is that the call fails, not that it cannot be
        written. `aggregate_by_tag` still defends against a half-pair that
        reaches the table by some OTHER route (a migration, a manual edit, a
        row from before this check existed) by counting it separately as
        `malformed_rows` rather than crashing or silently folding it.
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
        # Both-or-neither, enforced rather than merely followed: task_tag and
        # task_tag_source are one resolved fact (a value and its provenance),
        # not two independent optional legs like the cache tokens above, so a
        # call supplying exactly one is a bug -- raise instead of writing a
        # silently-incomplete row.
        if (task_tag is None) != (task_tag_source is None):
            raise ValueError(
                "task_tag and task_tag_source must be supplied together or "
                f"not at all; got task_tag={task_tag!r}, "
                f"task_tag_source={task_tag_source!r}"
            )
        if task_tag is not None:
            item["task_tag"] = task_tag
            item["task_tag_source"] = task_tag_source
        self._table.put_item(Item=item)
        return item

    def aggregate_by_tag(
        self,
        *,
        tenant_id: str,
        period: str,
        user_id: Optional[str] = None,
        max_pages: int = MAX_AGGREGATE_PAGES,
    ) -> TagAggregate:
        """Fold one tenant's `period` ('YYYY-MM') of UsageLogs into totals by
        (user_id, task_tag). `user_id`, when given, restricts to that one
        member (a `FilterExpression` over the tenant-partition query below,
        not a second index — the read stays one query per page either way);
        absent, it aggregates the whole tenant.

        Reads the tenant partition (PK query, `timestamp_log_id` bounded to
        the period) page by page and folds in memory — the read this
        aggregation needs is inherently unbounded for a tenant with enough
        history, so `max_pages` stops it after a fixed number of pages rather
        than after a fixed amount of WORK, and `truncated=True` says so rather
        than silently returning a partial total that looks complete.

        A row with neither `task_tag` nor `task_tag_source` predates this
        pair of attributes; it is counted in `legacy_rows` and never folded
        into a row under the unasserted-tag sentinel (see `record`). A row
        with EXACTLY ONE of the two (which `record` now refuses to write,
        but which can still reach the table by another route) is counted in
        `malformed_rows` instead: a half-written row is
        corruption to surface, not history to absorb into either count.

        `max_pages < 1` reads no page at all -- checked
        BEFORE the first query rather than after it (the loop below would
        otherwise always read at least one page regardless of the bound).
        Data remains unread in that case, so this is `truncated=True` with
        `pages_read=0`, not an empty-but-complete answer.
        """
        if max_pages < 1:
            return TagAggregate(
                rows=(), truncated=True, pages_read=0, legacy_rows=0, malformed_rows=0,
            )
        start = f"{period}-01T00:00:00"
        end = f"{_next_period(period)}-01T00:00:00"
        # `timestamp_log_id` is `{iso}#{log_id}`. `start`/`end` above are bare
        # date-time strings with NO UTC offset suffix, while `_now_iso()`'s
        # output always carries one (`+00:00`, even when the microsecond
        # fraction is exactly zero and `isoformat()` omits `.000000`
        # entirely -- it never omits the offset). A bare string is a strict
        # PREFIX of any real timestamp built from it, and a prefix always
        # sorts before the longer string it prefixes, so every real row in
        # `period` sorts strictly between `start` and `end` regardless of its
        # microsecond fraction, and BETWEEN's inclusive ends land exactly on
        # the month boundary. A KeyConditionExpression may carry only ONE
        # range condition per key attribute (DynamoDB rejects two, e.g. a
        # separate `gte` AND `lt` on the same sort key), which is why this is
        # BETWEEN and not that.
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("tenant_id").eq(tenant_id)
                & Key("timestamp_log_id").between(start, end)
            ),
        }
        if user_id is not None:
            kwargs["FilterExpression"] = Attr("user_id").eq(user_id)

        totals: dict[tuple[str, str], dict[str, int]] = {}
        legacy_rows = 0
        malformed_rows = 0
        pages_read = 0
        truncated = False
        while True:
            resp = self._table.query(**kwargs)
            pages_read += 1
            for it in resp.get("Items", []):
                tag = it.get("task_tag")
                source = it.get("task_tag_source")
                if tag is None and source is None:
                    legacy_rows += 1
                    continue
                if tag is None or source is None:
                    malformed_rows += 1
                    continue
                key = (str(it.get("user_id") or ""), str(tag))
                row = totals.setdefault(
                    key,
                    {"requests": 0, "absent_count": 0, "dropped_grammar_count": 0,
                     "cost_microusd": 0, "requests_without_cost": 0,
                     "input_tokens": 0, "output_tokens": 0},
                )
                row["requests"] += 1
                if source == "absent":
                    row["absent_count"] += 1
                elif source in ("dropped_grammar", "dropped_reserved"):
                    row["dropped_grammar_count"] += 1
                # Counted from the SAME item, at the same place absence becomes zero,
                # so the sum and the count cannot disagree about which rows they saw.
                # `is None` rather than a falsy test: a genuine zero cost is a priced
                # request that happened to round to nothing, not an unpriced one.
                if it.get("cost_microusd") is None:
                    row["requests_without_cost"] += 1
                row["cost_microusd"] += int(it.get("cost_microusd", 0) or 0)
                row["input_tokens"] += int(it.get("input_tokens", 0) or 0)
                row["output_tokens"] += int(it.get("output_tokens", 0) or 0)
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            if pages_read >= max_pages:
                truncated = True
                break
            kwargs["ExclusiveStartKey"] = last_key

        rows = tuple(
            TagAggregateRow(
                user_id=uid,
                task_tag=tag,
                requests=v["requests"],
                absent_count=v["absent_count"],
                dropped_grammar_count=v["dropped_grammar_count"],
                cost_microusd=v["cost_microusd"],
                requests_without_cost=v["requests_without_cost"],
                input_tokens=v["input_tokens"],
                output_tokens=v["output_tokens"],
            )
            for (uid, tag), v in totals.items()
        )
        return TagAggregate(
            rows=rows, truncated=truncated, pages_read=pages_read,
            legacy_rows=legacy_rows, malformed_rows=malformed_rows,
        )
