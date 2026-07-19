"""TenantBudgets: dollar pool shared across all users of a tenant.

Layout:
    PK = tenant_id, SK = "BUDGET#<period>"  (period e.g. "2026-07")
    attributes:
        pool_limit_microusd     : hard ceiling for the period (int micro-USD)
        pool_headroom_microusd  : remaining budget = limit - reserved - settled.
                                  THE single counter the reserve gate reads/writes.
        pool_reserved_microusd  : sum of in-flight reservations not yet settled
                                  (mirror, for the read API + audit reconciliation)
        pool_settled_microusd   : sum of settled (actual) spend (mirror)
        status                  : "active" | "suspended"
        version                 : schema/version marker

Invariant enforced at reserve time (inside a DynamoDB transaction):

    pool_headroom >= amount  AND  status = active      (headroom -= amount)

which is exactly `limit - reserved - settled >= amount` since
`headroom == limit - reserved - settled` is maintained on every write. The
reserve is a SINGLE conditional `ADD` to `pool_headroom_microusd`, with the
condition referencing ONLY the counter being mutated (no read-back snapshot of
reserved+settled). That kills the failure mode the old design collapsed on: the
snapshot-all-equal CAS made every concurrent reserve on a hot row invalidate the
others' snapshot, so a burst produced a `ConditionalCheckFailed` storm. With a
headroom condition, a concurrent reserve that still fits does NOT fail this
condition, so that storm is gone and a pool-item `ConditionalCheckFailed` now
means the budget is genuinely exhausted (→ HTTP 402 `tenant_pool_exhausted`, not
retried).

This does NOT make reserve retry-free: the item is composed into a
`TransactWriteItems` with the HOLD put + per-user debit, so two reserves on the
SAME pool row can still be serialized by DynamoDB and one cancelled with reason
`TransactionConflict` (a transaction-layer collision, distinct from this item's
condition). The caller (reserve_credit) still runs a bounded retry loop, but now
retries ONLY on `TransactionConflict`/throttling — rarer and self-clearing — and
never on a pool `ConditionalCheckFailed`. So the headroom design removes the
snapshot-invalidation storm; it does not claim first-try success under all
single-row contention. `pool_reserved`/`pool_settled` are kept as
unconditional-ADD mirrors so the read surface and the audit still hold. See
docs/design/ledger-hot-path.md for the rationale and the benchmark that motivated
this.

A tenant with no BUDGET row for the period is *unlimited at the pool level*:
the pipeline then falls back to per-user token budgeting only, preserving the
pre-pool behaviour. Pool budgeting is opt-in per tenant/period.

All amounts are integer micro-USD; this module never introduces a float.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, tenant_budgets_table_name

# set_pool_limit is a conditional CAS on the pool ceiling (Fable review finding
# 3). Concurrent admin writes to the SAME period's ceiling are rare, so a small
# bounded retry is plenty; exceeding it is a genuine anomaly worth surfacing.
_SET_LIMIT_MAX_RETRIES = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_compact_budget(obj: Any) -> str:
    """Deterministic compact JSON (sorted keys) for freezing a rate_snapshot onto
    the HOLD row. Matches credit_ledger._json_compact so a rehydrate reads back
    byte-identically regardless of which writer produced it."""
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def budget_sk(period: str) -> str:
    return f"BUDGET#{period}"


def hold_sk_prefix(period: str) -> str:
    """SK prefix that groups a period's per-reservation hold items under the
    tenant's partition (so they can be range-Queried)."""
    return f"HOLD#{period}#"


# Width of the zero-padded epoch-seconds field embedded in a hold's SK. Ten
# digits covers all epochs through the year 2286, so lexical SK order == expiry
# order for the lifetime of this system. The reaper relies on that ordering: it
# range-scans holds whose embedded expiry is <= now, which lets DynamoDB's Limit
# bound the scan *by expiry* (oldest orphans first) instead of by arbitrary key
# order — the fix for the "orphan buried behind live holds, never swept" leak.
_EXPIRES_WIDTH = 10


def hold_sk(period: str, expires_at_epoch: int, hold_id: str) -> str:
    """Build a hold's sort key with the expiry embedded so SK order is expiry
    order: ``HOLD#<period>#<expires_at:010d>#<hold_id>``."""
    return f"{hold_sk_prefix(period)}{int(expires_at_epoch):0{_EXPIRES_WIDTH}d}#{hold_id}"


def hold_sk_expiry_ceiling(period: str, now_epoch: int) -> str:
    """Upper bound (inclusive) for a range scan of holds expired at/-before
    `now_epoch`: every SK whose embedded expiry is <= now sorts <= this string.

    The trailing high sentinel (``#￿``) makes the bound inclusive of the
    whole `now_epoch` second regardless of the hold_id suffix.
    """
    return f"{hold_sk_prefix(period)}{int(now_epoch):0{_EXPIRES_WIDTH}d}#￿"


def current_period() -> str:
    """Return the current billing period key (calendar month, UTC)."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def previous_period(period: str) -> str:
    """Return the calendar month immediately before `period` ("2026-07"->"2026-06").

    The reaper sweeps this alongside the current period so a hold orphaned by a
    crash in the final moments of a month is still reclaimed after the boundary
    rolls over (otherwise last month's `pool_reserved` would stay inflated and
    the hold row would linger forever, since native TTL is intentionally unused).
    """
    year, month = (int(x) for x in period.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


class TenantBudgetsRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._name = table_name or tenant_budgets_table_name()
        self._table = get_dynamodb_resource().Table(self._name)

    @property
    def table_name(self) -> str:
        return self._name

    # ----- read -----
    def get(
        self, tenant_id: str, period: str, *, consistent_read: bool = False
    ) -> Optional[dict[str, Any]]:
        """Read a tenant's pool row for a period.

        `consistent_read=True` forces a strongly-consistent GetItem, used by the
        reserve loop so the optimistic snapshot lock is taken against the
        current counters (a stale read makes the equality condition fail
        forever). Admin/read-only callers keep the cheaper eventually-consistent
        default.
        """
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
            ConsistentRead=consistent_read,
        )
        return resp.get("Item")

    def get_hold(
        self, *, tenant_id: str, sk: str, consistent_read: bool = True
    ) -> Optional[dict[str, Any]]:
        """Strongly-consistent read of one hold row by exact `sk` (or None).

        Used by the external-authorize rehydrate path to confirm the hold still
        exists (not yet captured/voided/reclaimed) and read its `amount_microusd`.
        ConsistentRead by default so a capture immediately after authorize sees
        its own just-written hold."""
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": sk},
            ConsistentRead=consistent_read,
        )
        return resp.get("Item")

    def pool_summary(self, tenant_id: str, period: str) -> Optional[dict[str, int]]:
        """Return the pool's limit/reserved/settled/remaining in micro-USD,
        or None when the tenant has no pool budget for the period.
        """
        item = self.get(tenant_id, period)
        if not item:
            return None
        limit = int(item.get("pool_limit_microusd", 0))
        reserved = int(item.get("pool_reserved_microusd", 0))
        settled = int(item.get("pool_settled_microusd", 0))
        # `remaining` is reported from the authoritative headroom counter when it
        # exists (a row written/backfilled under the new scheme), else derived
        # from the mirrors (a legacy row not yet backfilled). They are equal by
        # the maintained invariant; preferring headroom keeps the read consistent
        # with the gate the reserve actually checks.
        if "pool_headroom_microusd" in item:
            remaining = max(int(item.get("pool_headroom_microusd", 0)), 0)
        else:
            remaining = max(limit - reserved - settled, 0)
        return {
            "pool_limit_microusd": limit,
            "pool_reserved_microusd": reserved,
            "pool_settled_microusd": settled,
            "pool_headroom_microusd": int(item.get("pool_headroom_microusd",
                                                   limit - reserved - settled)),
            "remaining_microusd": remaining,
            "status": item.get("status", "active"),
        }

    # ----- write (admin) -----
    def set_pool_limit(
        self,
        *,
        tenant_id: str,
        period: str,
        pool_limit_microusd: int,
        status: str = "active",
    ) -> dict[str, Any]:
        """Create or update a tenant's pool limit for a period.

        Preserves the running `pool_reserved`/`pool_settled` counters if the
        row already exists (so changing the ceiling mid-period does not reset
        spend); initialises them to 0 on first creation. `pool_headroom` is
        shifted by the *ceiling delta* (`new_limit - old_limit`) so the invariant
        `headroom == limit - reserved - settled` is preserved without ever
        touching the reserved/settled mirrors; raising or lowering the limit
        shifts headroom by the same delta (a lower limit can make headroom
        negative, which correctly refuses all new admissions).

        RACE-SAFETY (Fable review, finding 3): this used to be a read-then-put
        that rewrote headroom from the read-back mirrors, so a reserve/settle
        landing between the read and the put silently lost its headroom move
        (a real over-admission / under-admission window). It is now a single
        CONDITIONAL UpdateItem: `SET pool_limit = :new ADD pool_headroom :delta`
        guarded by `pool_limit = :old`, so a concurrent reserve's headroom ADD
        composes with (never clobbers) this one — DynamoDB serializes them. A
        `ConditionalCheckFailed` here means the limit moved under us (another
        admin write); we re-read and retry a small number of times. Creation is
        an `attribute_not_exists(tenant_id)` seed. This path is an admin write
        (create pool / change ceiling), expected to be rare.
        """
        new_limit = int(pool_limit_microusd)
        for _attempt in range(_SET_LIMIT_MAX_RETRIES):
            existing = self.get(tenant_id, period)
            if existing is None:
                # First creation: seed the row iff nobody else just created it.
                headroom = new_limit  # reserved = settled = 0 at creation
                try:
                    self._table.put_item(
                        Item={
                            "tenant_id": tenant_id,
                            "sk": budget_sk(period),
                            "pool_limit_microusd": Decimal(new_limit),
                            "pool_headroom_microusd": Decimal(headroom),
                            "pool_reserved_microusd": Decimal(0),
                            "pool_settled_microusd": Decimal(0),
                            # PENDING protocol per-hold marker map (docs/design/
                            # pending-protocol.md). Empty at creation; the
                            # non-transactional reserve SETs applied.<hold_id> in
                            # the same UpdateItem as the debit. Seeded here so the
                            # nested SET has a map to write into. Inert while the
                            # protocol flag is off (nothing writes it).
                            "applied": {},
                            "status": status,
                            "version": "2",
                            "updated_at": _now_iso(),
                        },
                        ConditionExpression="attribute_not_exists(tenant_id)",
                    )
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                        continue  # someone created it first → fall through to update
                    raise
                return self.get(tenant_id, period) or {}

            if "pool_headroom_microusd" not in existing:
                # LEGACY row (migration step 1 / repair): no headroom attribute
                # yet. SET it to the invariant value from the row's OWN live
                # mirrors, guarded by `attribute_not_exists(pool_headroom)` so a
                # concurrent write that just created headroom is never clobbered.
                # This is the only branch that SETs (rather than ADDs) headroom,
                # and it only ever fires on a row that has none — so it cannot
                # overwrite a live reserve's headroom move.
                reserved = int(existing.get("pool_reserved_microusd", 0))
                settled = int(existing.get("pool_settled_microusd", 0))
                headroom = new_limit - reserved - settled
                try:
                    self._table.update_item(
                        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                        UpdateExpression=(
                            "SET pool_limit_microusd = :new, "
                            "pool_headroom_microusd = :h, #st = :status, "
                            "version = :ver, updated_at = :now"
                        ),
                        ConditionExpression="attribute_not_exists(pool_headroom_microusd)",
                        ExpressionAttributeNames={"#st": "status"},
                        ExpressionAttributeValues={
                            ":new": Decimal(new_limit),
                            ":h": Decimal(headroom),
                            ":status": status,
                            ":ver": "2",
                            ":now": _now_iso(),
                        },
                    )
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                        continue  # headroom appeared under us → re-read, take ADD branch
                    raise
                return self.get(tenant_id, period) or {}

            old_limit = int(existing.get("pool_limit_microusd", 0))
            delta = new_limit - old_limit
            try:
                # Row already carries headroom. Shift it by the ceiling delta
                # only. Never SET headroom or touch the reserved/settled mirrors,
                # so a concurrent reserve's `ADD pool_headroom :neg` composes with
                # this `ADD :delta` (DynamoDB serializes the two ADDs).
                self._table.update_item(
                    Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                    UpdateExpression=(
                        "SET pool_limit_microusd = :new, #st = :status, "
                        "version = :ver, updated_at = :now "
                        "ADD pool_headroom_microusd :delta"
                    ),
                    ConditionExpression="pool_limit_microusd = :old",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":new": Decimal(new_limit),
                        ":old": Decimal(old_limit),
                        ":delta": Decimal(delta),
                        ":status": status,
                        ":ver": "2",
                        ":now": _now_iso(),
                    },
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue  # limit moved under us → re-read and retry
                raise
            return self.get(tenant_id, period) or {}

        raise RuntimeError(
            f"set_pool_limit: lost the limit CAS {_SET_LIMIT_MAX_RETRIES}x for "
            f"{tenant_id}/{period}; concurrent admin writes to the same pool ceiling"
        )

    def reconcile_headroom(self, tenant_id: str, period: str) -> dict[str, Any]:
        """Repair `pool_headroom` to the invariant `limit - reserved - settled`,
        race-safely, whatever value it currently holds. This is the migration /
        self-heal primitive (Fable review finding 2).

        WHY value-repair, not presence-seed: during a rolling deploy a new-code
        `settle` can fire on a not-yet-backfilled row. Its unconditional
        `ADD pool_headroom :dh` CREATES the attribute at `(reserved - actual)` —
        a WRONG value (short by `limit - reserved - settled`). A presence-gated
        backfill (`if headroom absent`) would then see the attribute present and
        skip the row forever, cementing the wrong value. So the reconcile keys on
        the VALUE: it recomputes the target from the always-correct mirrors
        (`pool_reserved`/`pool_settled` are unconditional ADDs, correct in both
        the old and new code) and writes it iff the stored headroom still differs.

        Race-safety: guarded by `attribute_not_exists(pool_headroom) OR
        pool_headroom = :observed` — i.e. write only if headroom is still the
        (absent-or-wrong) value we just read, so a concurrent reserve/settle that
        moved headroom in between is never clobbered (we simply re-read and the
        drift may already be gone). Returns the reconciled row. Idempotent: a row
        already at the invariant is left untouched.
        """
        for _attempt in range(_SET_LIMIT_MAX_RETRIES):
            item = self.get(tenant_id, period)
            if item is None:
                return {}
            limit = int(item.get("pool_limit_microusd", 0))
            reserved = int(item.get("pool_reserved_microusd", 0))
            settled = int(item.get("pool_settled_microusd", 0))
            target = limit - reserved - settled
            has_headroom = "pool_headroom_microusd" in item
            observed = int(item["pool_headroom_microusd"]) if has_headroom else None
            if has_headroom and observed == target:
                return item  # already at the invariant — nothing to do
            values: dict[str, Any] = {
                ":h": Decimal(target),
                ":now": _now_iso(),
            }
            if has_headroom:
                cond = "pool_headroom_microusd = :observed"
                values[":observed"] = Decimal(observed)
            else:
                cond = "attribute_not_exists(pool_headroom_microusd)"
            try:
                self._table.update_item(
                    Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                    UpdateExpression="SET pool_headroom_microusd = :h, updated_at = :now",
                    ConditionExpression=cond,
                    ExpressionAttributeValues=values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue  # headroom moved under us → re-read; drift may be gone
                raise
            return self.get(tenant_id, period) or {}

        raise RuntimeError(
            f"reconcile_headroom: lost the headroom CAS {_SET_LIMIT_MAX_RETRIES}x "
            f"for {tenant_id}/{period}; sustained concurrent writes to one pool row"
        )

    # ----- transaction item builders -----
    # These return the Update fragments the pipeline composes into a single
    # TransactWriteItems alongside the per-user balance debit. Building them
    # here keeps the pool's ConditionExpression in one place.

    def reserve_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        amount_microusd: int,
    ) -> dict[str, Any]:
        """Transaction item that reserves `amount_microusd` from the pool.

        A SINGLE conditional counter operation: subtract `amount` from
        `pool_headroom_microusd` iff headroom still covers it and the pool is
        active. There is NO snapshot pre-read of the counter — the condition
        references only the counter being mutated (`pool_headroom >= amount`),
        NOT a read-back snapshot of reserved+settled. That is the whole point:
        the old snapshot CAS made every concurrent reserve on a hot pool row
        invalidate the others' snapshot (a `ConditionalCheckFailed` storm — the
        measured p99 collapse); with a headroom condition, a concurrent reserve
        that still fits does NOT fail this item's condition, so the retry storm
        driven by snapshot invalidation is gone.

        What this does NOT eliminate: this item is composed into a
        `TransactWriteItems` alongside the HOLD put and the per-user debit, so
        two reserves touching the SAME pool row can still collide at the
        transaction layer and one is cancelled with reason `TransactionConflict`
        (optimistic serialization of the transaction, distinct from this item's
        `ConditionalCheckFailed`). The caller therefore STILL retries — but only
        on `TransactionConflict`/throttling, which is rarer and self-clearing —
        and maps a pool-item `ConditionalCheckFailed` to HTTP 402
        `tenant_pool_exhausted` (genuine exhaustion, not retried). See the
        cancellation-reason branch in `reserve_credit` (mvp/_pipeline.py): pool
        `ConditionalCheckFailed` -> 402, `TransactionConflict` -> retry.

        The `pool_reserved_microusd` mirror is incremented in the same update so
        the read API and the `headroom == limit - reserved - settled` audit stay
        consistent. `status = active` gates suspended pools; a legacy row without
        `pool_headroom_microusd` fails the `attribute_exists(pool_headroom_microusd)`
        guard (a not-yet-backfilled pool must be backfilled before it can reserve
        under this scheme, rather than silently admitting on a missing counter).
        """
        return {
            "Update": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": budget_sk(period)},
                },
                "UpdateExpression": (
                    "ADD pool_headroom_microusd :neg, pool_reserved_microusd :amt "
                    "SET updated_at = :now"
                ),
                "ConditionExpression": (
                    "attribute_exists(pool_headroom_microusd) AND #st = :active AND "
                    "pool_headroom_microusd >= :amt"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":amt": {"N": str(int(amount_microusd))},
                    ":neg": {"N": str(-int(amount_microusd))},
                    ":active": {"S": "active"},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    def settle_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        reserved_microusd: int,
        actual_microusd: int,
    ) -> dict[str, Any]:
        """Transaction item that settles a reservation against actual spend.

        Moves `reserved` out of `pool_reserved` and `actual` into `pool_settled`,
        and returns the net to headroom: `headroom += (reserved - actual)` — the
        reservation is released and the true spend is deducted, so the invariant
        `headroom == limit - reserved - settled` is preserved. Unconditional (no
        retry): settlement must never fail a live request (a refund/top-up cannot
        exceed the pool by construction because the original reserve already fit).
        """
        delta_reserved = -int(reserved_microusd)
        # release the hold's reservation, deduct the actual spend
        delta_headroom = int(reserved_microusd) - int(actual_microusd)
        return {
            "Update": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": budget_sk(period)},
                },
                "UpdateExpression": (
                    "ADD pool_reserved_microusd :dr, "
                    "pool_settled_microusd :actual, "
                    "pool_headroom_microusd :dh SET updated_at = :now"
                ),
                "ExpressionAttributeValues": {
                    ":dr": {"N": str(delta_reserved)},
                    ":actual": {"N": str(int(actual_microusd))},
                    ":dh": {"N": str(delta_headroom)},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    # ----- hold items (orphan-reservation reaper) -----
    # Every in-flight reservation writes a sibling HOLD row in the same
    # transaction as the aggregate `pool_reserved += cost`. The HOLD records how
    # much *this specific request* is holding and when the hold expires. settle
    # and release delete the HOLD in the same transaction that decrements the
    # aggregate, so a HOLD outlives its reservation only when the process died
    # between reserve and settle (task kill / OOM / drain). The lazy sweep then
    # reclaims those orphans: `pool_reserved -= amount` plus a conditional
    # Delete(hold), the condition making the reclaim idempotent (a HOLD is
    # reclaimed at most once, so the aggregate can never be double-subtracted or
    # driven negative). This is the ONLY reaper — native DynamoDB TTL is
    # deliberately NOT used on HOLDs, because a TTL delete would drop the row
    # without decrementing the aggregate, converting a transient leak into a
    # permanent one.

    def hold_put_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        hold_id: str,
        amount_microusd: int,
        expires_at_epoch: int,
        source: Optional[str] = None,
        description: Optional[str] = None,
        rate_snapshot: Optional[dict[str, Any]] = None,
        payload_hash: Optional[str] = None,
        run_id: Optional[str] = None,
        run_id_is_fallback: bool = False,
    ) -> dict[str, Any]:
        """Transaction item that records a per-reservation hold.

        Written in the SAME TransactWriteItems as the aggregate reserve, so a
        hold exists iff its share of `pool_reserved_microusd` is outstanding.
        The SK embeds the expiry (see `hold_sk`) so the reaper can range-scan by
        expiry; `attribute_not_exists(sk)` guards against a hold_id collision.

        Enrichment (two-item migration, docs/design/ledger-hot-path.md step 2):
        the HOLD row is promoted to the synchronous source of truth so
        capture/void can read it ALONE instead of the (soon-async) RESERVE event.
        Optional, additive attributes, written only when supplied:

          * `source` — "external" | "inline". The C-1 security gate reads this
            (an inline LLM hold's token must never be capturable/voidable). The
            gate defaults DENY on a MISSING attribute, so a legacy hold written
            before this enrichment is not capturable via the external API — the
            same fail-closed answer as a bogus token.
          * `description` / `rate_snapshot` — frozen here so an external capture
            in a separate HTTP call rehydrates from the HOLD alone.
          * `payload_hash` — the authorize request fingerprint, so a duplicate
            Idempotency-Key that resolves to this hold can 422 on a different body.

        Inline holds pass `source="inline"` (and nothing else); external authorize
        passes the full set. Absent args are simply not written (no None in DDB).
        """
        item: dict[str, Any] = {
            "tenant_id": {"S": tenant_id},
            "sk": {"S": hold_sk(period, expires_at_epoch, hold_id)},
            "hold_id": {"S": hold_id},
            "period": {"S": period},
            "amount_microusd": {"N": str(int(amount_microusd))},
            "expires_at": {"N": str(int(expires_at_epoch))},
            "created_at": {"S": _now_iso()},
        }
        if source:
            item["source"] = {"S": str(source)}
        if description:
            item["hold_description"] = {"S": str(description)}
        if payload_hash:
            item["payload_hash"] = {"S": str(payload_hash)}
        if rate_snapshot is not None:
            item["rate_snapshot"] = {"S": _json_compact_budget(rate_snapshot)}
        if run_id:
            # Run attribution so a HOLD-only rehydrate keys the SETTLE's run-index
            # the SAME way the RESERVE event did. The fallback marker mirrors the
            # RESERVE event's run_id_source: a hold reserved WITHOUT a real
            # workflow_run_id stored run_id=hold_id and must NOT resurface that
            # synthetic id as a real run on settle.
            item["run_id"] = {"S": str(run_id)}
            if run_id_is_fallback:
                item["run_id_source"] = {"S": "hold_id_fallback"}
        return {
            "Put": {
                "TableName": self._name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(sk)",
            }
        }

    # ----- PENDING protocol primitives (docs/design/pending-protocol.md) -----
    # The non-transactional hot-path reserve, gated behind
    # STRATOCLAVE_RESERVE_PROTOCOL=pending (default off). These are separate
    # single-item writes, NOT transaction fragments — the whole point is to avoid
    # TransactWriteItems on the hot pool row (the measured ~1,190 ms c=16 tail).
    # Every existing reader learns `status` semantics (absent == ACTIVE) FIRST, so
    # these are inert until the flag is flipped per-tenant.

    def hold_put_pending(
        self,
        *,
        tenant_id: str,
        period: str,
        hold_id: str,
        amount_microusd: int,
        expires_at_epoch: int,
        source: Optional[str] = None,
        description: Optional[str] = None,
        rate_snapshot: Optional[dict[str, Any]] = None,
        payload_hash: Optional[str] = None,
        run_id: Optional[str] = None,
        run_id_is_fallback: bool = False,
    ) -> None:
        """Step 1 of the PENDING protocol: Put a HOLD with ``status=PENDING``,
        uncontended, ``attribute_not_exists(sk)``. The WRITE-AHEAD INTENT — it MUST
        precede the pool debit so every debit has a discoverable HOLD record.
        Returns nothing; raises the client's ConditionalCheckFailedException on a
        duplicate sk (which, because ``hold_id`` is derived from the
        Idempotency-Key, is the duplicate-Key detector = idempotency anchor I6).

        Carries the same enrichment as ``hold_put_txn_item`` so capture/void can
        rehydrate from the HOLD alone. The ONLY difference from the transactional
        builder is the explicit ``status`` attribute (the transactional HOLD is
        implicitly ACTIVE = absent status). Uses the resource API (plain values,
        auto-serialized) so it always binds to the same session as the repo."""
        item: dict[str, Any] = {
            "tenant_id": tenant_id,
            "sk": hold_sk(period, expires_at_epoch, hold_id),
            "hold_id": hold_id,
            "period": period,
            "amount_microusd": int(amount_microusd),
            "expires_at": int(expires_at_epoch),
            "created_at": _now_iso(),
            "status": "PENDING",
        }
        if source:
            item["source"] = str(source)
        if description:
            item["hold_description"] = str(description)
        if payload_hash:
            item["payload_hash"] = str(payload_hash)
        if rate_snapshot is not None:
            item["rate_snapshot"] = _json_compact_budget(rate_snapshot)
        if run_id:
            item["run_id"] = str(run_id)
            if run_id_is_fallback:
                item["run_id_source"] = "hold_id_fallback"
        self._table.put_item(Item=item, ConditionExpression=Attr("sk").not_exists())

    # Sentinel returned by pool_reserve_update to distinguish the three outcomes
    # of the marker-carrying conditional UpdateItem.
    RESERVE_APPLIED = "applied"        # the debit committed on THIS call (200)
    RESERVE_ALREADY = "already"        # this hold's marker already present (idempotent)
    RESERVE_EXHAUSTED = "exhausted"    # genuine budget exhaustion (402)

    def pool_reserve_update(self, *, tenant_id: str, period: str, hold_id: str,
                            amount_microusd: int, table=None) -> str:
        """Step 2 of the PENDING protocol = THE COMMIT POINT. A single conditional
        ``UpdateItem`` on the pool row (NOT a transaction) that ATOMICALLY debits
        the counter AND records a per-hold marker ``applied.<hold_id> = amount``.
        Multi-attribute updates to ONE item are unconditionally atomic in DynamoDB,
        so this stays a single non-transactional write (the ~88 ms p99 property is
        preserved) — no ``TransactionConflict`` failure mode.

        The marker is what makes the debit LOCALLY OBSERVABLE and the write
        IDEMPOTENT (docs/design/pending-protocol.md, Fable marker design). Returns
        one of three outcomes, resolving the ambiguity that a bare ADD could not:

          * RESERVE_APPLIED   — condition held, counter debited + marker written.
          * RESERVE_ALREADY   — CCF but ALL_OLD shows this hold's marker already
            present: a prior attempt of THIS hold committed (a retry / lost-ack).
            The debit is a fact; treat as success, do NOT re-debit (I4 — the write
            is idempotent by construction, so an SDK auto-retry is harmless).
          * RESERVE_EXHAUSTED — CCF with no marker: genuine budget exhaustion (402).

        Condition: ``headroom >= amount AND active AND attribute_not_exists(
        applied.<hold_id>)``. On CCF we read ALL_OLD to pick ALREADY vs EXHAUSTED.
        `table` (optional) is a no-retry table; the marker makes retries safe, but
        an explicit single attempt keeps the ALL_OLD inspection deterministic."""
        tbl = table if table is not None else self._table
        try:
            tbl.update_item(
                Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                UpdateExpression=(
                    "ADD pool_headroom_microusd :neg, pool_reserved_microusd :amt "
                    "SET updated_at = :now, applied.#hid = :amt"
                ),
                ConditionExpression=(
                    "attribute_exists(pool_headroom_microusd) AND #st = :active AND "
                    "pool_headroom_microusd >= :amt AND attribute_not_exists(applied.#hid)"
                ),
                ExpressionAttributeNames={"#st": "status", "#hid": hold_id},
                ExpressionAttributeValues={
                    ":amt": int(amount_microusd),
                    ":neg": -int(amount_microusd),
                    ":active": "active",
                    ":now": _now_iso(),
                },
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return self.RESERVE_APPLIED
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            # ALL_OLD is on the exception under ReturnValuesOnConditionCheckFailure.
            # NOTE: even from a resource Table, the CCF exception's `Item` is the
            # LOW-LEVEL (DynamoDB-JSON) shape — `{"applied": {"M": {hid: {"N": ..}}}}`
            # — not resource-deserialized. Read the marker at the low-level path.
            old = e.response.get("Item", {}) or {}
            applied = (old.get("applied") or {}).get("M") or {}
            if hold_id in applied:
                return self.RESERVE_ALREADY   # our debit is already a fact (idempotent)
            return self.RESERVE_EXHAUSTED     # no marker -> genuine exhaustion (402)

    def pool_credit_back(self, *, tenant_id: str, period: str, hold_id: str) -> bool:
        """Exactly-once credit-back for the PENDING protocol: a single conditional
        ``UpdateItem`` that ATOMICALLY removes this hold's ``applied`` marker and
        returns its amount to the counter — conditional on the marker STILL being
        present. The marker's existence is the exact same-value predicate for "this
        hold's amount is currently debited from the pool", so double-credit is
        structurally impossible (the second call CCFs on the absent marker).

        Uses `applied.<hold_id>` as the amount source so it can never over/under-
        return. Returns True if it credited, False if the marker was already gone
        (already credited / never debited) — both leak-safe, never oversell. This
        is the ONLY way credit-back happens under the PENDING protocol; crediting
        by reading a hold's `amount_microusd` (the old path) is forbidden here
        because it is not gated on the debit actually having happened."""
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                UpdateExpression=(
                    "SET pool_headroom_microusd = pool_headroom_microusd + applied.#hid, "
                    "pool_reserved_microusd = pool_reserved_microusd - applied.#hid, "
                    "updated_at = :now REMOVE applied.#hid"
                ),
                ConditionExpression="attribute_exists(applied.#hid)",
                ExpressionAttributeNames={"#hid": hold_id},
                ExpressionAttributeValues={":now": _now_iso()},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def ensure_applied_map(self, *, tenant_id: str, period: str) -> None:
        """Idempotently seed the ``applied`` marker map on a pool row that predates
        the PENDING protocol (created before it was added to the create branch).
        Conditional on the map being absent, so it never clobbers live markers and
        is a no-op on rows that already have it. Called once per (tenant, period)
        by the pending reserve path (cached), NOT on the hot path per request."""
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                UpdateExpression="SET applied = :empty",
                ConditionExpression="attribute_exists(tenant_id) AND attribute_not_exists(applied)",
                ExpressionAttributeValues={":empty": {}},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return  # already has the map (or no pool row) — nothing to do
            raise

    def pool_marker_amount(self, *, tenant_id: str, period: str, hold_id: str) -> Optional[int]:
        """ConsistentRead of this hold's `applied` marker amount, or None if absent.
        The local, decisive answer to 'did this hold's debit commit?' (A1 restored
        without a transaction). Used by replay + capture's helping path."""
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": budget_sk(period)}, ConsistentRead=True)
        applied = (resp.get("Item") or {}).get("applied") or {}
        v = applied.get(hold_id)
        return int(v) if v is not None else None

    def _status_transition(self, *, tenant_id: str, sk: str, frm: str, to: str) -> bool:
        """Conditional status transition ``frm -> to`` on a HOLD row. Returns True
        on success, False if the row was not in `frm` (a race lost). Resource API."""
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": sk},
                UpdateExpression="SET #st = :to",
                ConditionExpression="#st = :frm",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":to": to, ":frm": frm},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def hold_activate(self, *, tenant_id: str, sk: str) -> bool:
        """Step 3: PENDING -> ACTIVE, conditional on still PENDING (so it can never
        race a sweeper fence — A2 single-item serialization decides the winner).
        OFF the synchronous critical path. Returns False if already fenced/terminal
        (the caller MUST alert, never swallow — I-biz)."""
        return self._status_transition(tenant_id=tenant_id, sk=sk,
                                        frm="PENDING", to="ACTIVE")

    def fence_pending_expired(self, *, tenant_id: str, sk: str) -> bool:
        """Sweeper fence: PENDING -> EXPIRED_UNCREDITED, conditional on still
        PENDING. Touches the pool NOT AT ALL — the sweeper cannot know whether the
        debit committed (no hold_id capability), so it never credits back; a
        debited-but-fenced hold leaks (bounded) until the reconciler recovers it in
        aggregate. Crediting here would oversell an un-debited hold. Returns False
        if the row was activated/terminal first (the activate won the race)."""
        return self._status_transition(tenant_id=tenant_id, sk=sk,
                                        frm="PENDING", to="EXPIRED_UNCREDITED")

    def list_holds(self, *, tenant_id: str, period: str) -> list[dict[str, Any]]:
        """All HOLD rows for a tenant/period (any status), strongly consistent.
        Used by the reconciler to sum ACTIVE and detect in-flight PENDING. A full
        per-period hold scan is acceptable on the cold reconcile path."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("tenant_id").eq(tenant_id)
                & Key("sk").begins_with(hold_sk_prefix(period))
            ),
            "ConsistentRead": True,
        }
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    def retire_reclaimed_best_effort(self, *, tenant_id: str, sk: str) -> None:
        """Flip EXPIRED_UNCREDITED -> RECLAIMED (conditional) so it stops being
        rescanned. Best-effort; never raises."""
        try:
            self._status_transition(tenant_id=tenant_id, sk=sk,
                                    frm="EXPIRED_UNCREDITED", to="RECLAIMED")
        except Exception:  # noqa: BLE001
            pass

    def mark_pending_failed_best_effort(self, *, tenant_id: str, sk: str) -> None:
        """Optional leak-safe terminal a caller MAY write when step 2 DEFINITIVELY
        failed (ConditionalCheckFailed = budget exhausted, so nothing was debited):
        PENDING -> FAILED, conditional on still PENDING, pool untouched. Best-
        effort — the proof must NOT depend on it (a crash before this leaves the
        sweeper to fence the hold), it only spares the sweeper one pass. Never
        raises: a failure here just defers to the sweeper."""
        try:
            self._status_transition(tenant_id=tenant_id, sk=sk,
                                    frm="PENDING", to="FAILED")
        except Exception:  # noqa: BLE001
            pass

    def query_pending_expired_holds(
        self, *, tenant_id: str, period: str, now_epoch: int, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Expired holds still in ``status=PENDING`` (the sweeper's fence targets).
        Same expiry-embedded range scan as ``query_expired_holds`` (so Limit bounds
        by expiry, oldest first), filtered to PENDING. A filtered scan is
        acceptable here: the fence is a bounded background sweep, not the hot path."""
        resp = self._table.query(
            KeyConditionExpression=(
                Key("tenant_id").eq(tenant_id)
                & Key("sk").between(
                    hold_sk_prefix(period),
                    hold_sk_expiry_ceiling(period, now_epoch),
                )
            ),
            FilterExpression=Attr("status").eq("PENDING"),
            ConsistentRead=True,
            Limit=int(limit),
        )
        return resp.get("Items", [])

    def hold_delete_txn_item(
        self, *, tenant_id: str, sk: str, require_exists: bool = True
    ) -> dict[str, Any]:
        """Transaction item that deletes a hold by its exact `sk`.

        Composed alongside the aggregate settle/release so the hold and its
        aggregate share disappear together. With `require_exists=True` (the
        default) the Delete is gated on `attribute_exists(sk)`: this is the
        latch that keeps the paired aggregate decrement from applying twice. If
        the reaper already reclaimed this hold (and already returned its
        reserved share), the condition fails, the whole transaction cancels, and
        the caller falls back to recording spend WITHOUT decrementing reserved
        again — the fix for the settle/reclaim double-subtract.
        """
        item: dict[str, Any] = {
            "Delete": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": sk},
                },
            }
        }
        if require_exists:
            item["Delete"]["ConditionExpression"] = "attribute_exists(sk)"
        return item

    def reclaim_hold_txn_item(
        self, *, tenant_id: str, sk: str
    ) -> dict[str, Any]:
        """Transaction item that deletes an expired hold by exact `sk` *only if
        it still exists* AND its debit is known to have committed, so the paired
        aggregate decrement (credit-back) happens at most once and never on an
        un-debited hold.

        The sweep composes this Delete with an aggregate
        `pool_reserved_microusd -= amount`. Two conditions:
          * `attribute_exists(sk)` — idempotency latch (a concurrent sweep or late
            settle already removed it → the whole txn cancels, no double-subtract).
          * `status = ACTIVE OR attribute_not_exists(status)` — the PENDING-protocol
            credit gate (docs/design/pending-protocol.md, readers-first). A
            transactional (pre-PENDING) hold has NO status attribute, so
            `attribute_not_exists(status)` keeps this reaper byte-identical for
            today's data — it is INERT until PENDING holds exist. Once they do, a
            PENDING hold may be un-debited, so crediting it would oversell; the
            sweeper's `fence_pending_expired` handles those WITHOUT touching the
            pool, and this reaper only credits ACTIVE (known-debited) holds.
        """
        return {
            "Delete": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": sk},
                },
                "ConditionExpression": (
                    "attribute_exists(sk) AND "
                    "(#st = :active_h OR attribute_not_exists(#st))"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {":active_h": {"S": "ACTIVE"}},
            }
        }

    def query_expired_holds(
        self, *, tenant_id: str, period: str, now_epoch: int, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Return up to `limit` holds for the period whose embedded expiry has
        passed, **oldest-expiry first**. Strongly consistent so the sweep does
        not act on a stale view and try to reclaim a hold a settle just deleted.

        Because the SK embeds the (zero-padded) expiry, this is a pure key range
        scan — `between(prefix, expiry-ceiling(now))` — with NO FilterExpression.
        That matters: DynamoDB's `Limit` bounds items *evaluated*, and a filter
        is applied after. The previous begins_with + expires_at filter let `Limit`
        cut the page across live holds (arbitrary uuid order) so an expired
        orphan sitting behind `Limit` live holds was never returned and leaked
        forever. Ranging by embedded expiry makes `Limit` count only already-
        expired holds, oldest first, so bounded sweeps drain the backlog.
        """
        resp = self._table.query(
            KeyConditionExpression=(
                Key("tenant_id").eq(tenant_id)
                & Key("sk").between(
                    hold_sk_prefix(period),
                    hold_sk_expiry_ceiling(period, now_epoch),
                )
            ),
            ConsistentRead=True,
            Limit=int(limit),
        )
        return resp.get("Items", [])
