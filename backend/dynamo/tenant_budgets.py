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
        manual_limit_microusd   : the operator's own figure, in micro-USD.
                                  PRESENT (including `0`) means the ceiling is
                                  that figure. ABSENT means "follow the seat
                                  count". See "the ceiling rule" below.
        seat_count              : the tenant's active membership count, moved by
                                  the ONE seat-delta writer on every membership
                                  change, whether or not money moves with it.
        seat_rate_microusd      : the per-seat monthly rate IN FORCE for this
                                  row, stored so the ceiling is reproducible
                                  across a re-run and a rollback. A process
                                  configured with a different rate refuses to
                                  start (see `assert_seat_rate_in_force`).
        pool_granted_microusd   : an approved raise, added on top of the
                                  baseline. ABSENT until grants exist, and
                                  absence reads as zero -- which is why the
                                  identity below carries a coalesce from day one.
        version                 : schema/version marker

THE CEILING RULE (docs/design/limits.md section 4):

    seat_term  = seat_count x seat_rate
    baseline   = manual_limit  if manual_limit is PRESENT  else seat_term
    pool_limit = baseline + coalesce(pool_granted, 0)

**Absence** of `manual_limit_microusd` means "follow the seat count"; **zero is a
figure** and means zero budget. That asymmetry is load-bearing rather than
stylistic: `limit_usd_cents` accepts `0` today, meaning every request refused, so
reading `0` as "follow the seats" would silently reverse a legal input for every
existing caller. The sentinel is absence, which no existing caller can send, and
`{"follow_seats": true}` on the setter is how absence is asked for.

`pool_granted_microusd` is the mirror image, deliberately: it is reset by
OMISSION and never by writing an explicit zero, because for it absence and zero
mean the same thing (`ADD` on a missing numeric attribute creates it). Getting
those two backwards inverts the feature, so each one says which it is where it is
declared, in `dynamo.pool_row_schema.POOL_ROW_ATTRIBUTES` -- the ONE authority for
this row's shape, deliberately in its own module so the size guard cannot end up
measuring a shape the rollover and the reconciler do not use.

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

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, tenant_budgets_table_name
# The attribute NAMES, which are used in expressions and conditions throughout this
# module. The declaration's helpers are reached through the module (`pool_row_schema.
# carried_attributes()`) and deliberately NOT rebound here: a name bound in two
# modules is two places to read the row's shape from, which is the second authority
# this split exists to collapse.
from . import pool_row_schema
from .pool_row_schema import (
    GRANT_CAP_ATTR,
    MANUAL_LIMIT_ATTR,
    POOL_GRANTED_ATTR,
    SEAT_COUNT_ATTR,
    SEAT_RATE_ATTR,
)

# The tenant pool's per-seat monthly figure, as this PROCESS is configured. It is
# NOT the rate a row's ceiling was computed from: that one is stored on the row
# (`seat_rate_microusd`) so the ceiling is reproducible, and a process whose
# configuration disagrees with the stored rate refuses to start rather than
# recomputing ceilings at a figure nobody chose. `assert_seat_rate_in_force` is
# the check; `main.py` is the boot path that calls it.
_SEAT_MONTHLY_USD_DEFAULT = 200


def seat_monthly_usd() -> int:
    """The per-seat monthly rate in whole USD, as this process is configured."""
    return int(os.getenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_SEAT_MONTHLY_USD_DEFAULT)))


def seat_rate_microusd() -> int:
    """The same rate in integer micro-USD, which is the unit the row stores.

    One unit on the pool row, not two: every other money attribute on it is
    micro-USD, and a second unit on one item is a conversion waiting to be
    forgotten at the one call site that omits it.
    """
    return seat_monthly_usd() * 1_000_000


# ---------------------------------------------------------------------------
# The rule, as three pure functions over a row
# ---------------------------------------------------------------------------
def row_seat_rate_microusd(item: dict[str, Any]) -> int:
    """The rate this row's seat term is computed at: the stored one when the row
    carries it, else the rate this process is configured with.

    The fallback is reachable only on a row M1 has not touched yet, and it is
    safe there for the reason R20 exists: a process whose configured rate
    disagrees with the rate in force refuses to boot, so on any running process
    the two are the same number.
    """
    stored = (item or {}).get(SEAT_RATE_ATTR)
    return int(stored) if stored is not None else seat_rate_microusd()


def seat_term_microusd(item: dict[str, Any]) -> int:
    """`seat_count x seat_rate`, in micro-USD."""
    return int((item or {}).get(SEAT_COUNT_ATTR, 0)) * row_seat_rate_microusd(item)


def is_seat_tracked(item: dict[str, Any]) -> bool:
    """True iff this row follows the seat count -- i.e. the operator's figure is
    ABSENT. A stored `0` is a figure ("refuse everything") and returns False."""
    return MANUAL_LIMIT_ATTR not in (item or {})


def baseline_microusd(item: dict[str, Any]) -> int:
    """`manual_limit` when present (including zero), else the seat term."""
    if is_seat_tracked(item):
        return seat_term_microusd(item)
    return int(item[MANUAL_LIMIT_ATTR])


def granted_microusd(item: dict[str, Any]) -> int:
    """The granted term, zero until grants exist. The coalesce lives here, once,
    so the identity below is true from day one and F2 adds a writer rather than
    editing an invariant."""
    return int((item or {}).get(POOL_GRANTED_ATTR, 0))


def expected_pool_limit_microusd(item: dict[str, Any]) -> int:
    """`baseline + coalesce(granted, 0)` -- what `pool_limit_microusd` must equal.
    The reconciler's identity check compares the stored figure to this."""
    return baseline_microusd(item) + granted_microusd(item)


def grant_cap_microusd(item: dict[str, Any]) -> Optional[int]:
    """The operator's own aggregate grant cap, or None when the row carries none.

    None is a MEANING and not a missing value: it says "derive the cap from this
    row's baseline, now". Returned as None rather than coalesced to a number here
    so a caller that has to render the distinction -- a surface saying whether the
    cap is a figure somebody chose -- can, and so the two sentinels on this row
    stay visibly opposite. Absence of `manual_limit` means follow the seats;
    absence of the cap means follow the baseline; a stored zero in either case is
    a figure.
    """
    v = (item or {}).get(GRANT_CAP_ATTR)
    return None if v is None else int(v)


def effective_grant_cap_for_row(item: dict[str, Any]) -> int:
    """The cap in force for this row: the stored figure, else its baseline.

    A PURE function of the row, so the approval guard, the daily check and every
    read surface resolve the cap the same way from the same input. The
    alternative -- each caller reading the attribute and falling back on its own
    idea of the default -- is three defaults that agree until one of them is
    edited.

    Derived from `baseline_microusd` rather than from `pool_limit_microusd`,
    which matters: the limit already CONTAINS the granted term, so capping
    against it would let each approval enlarge the cap for the next one, and a
    tenant could walk its ceiling up without limit one grant at a time.
    """
    stored = grant_cap_microusd(item)
    return baseline_microusd(item) if stored is None else stored


class SeatRateMismatchError(RuntimeError):
    """Raised when this process's `STRATOCLAVE_SEAT_MONTHLY_USD` disagrees with
    the rate in force on the stored rows.

    Not a warning, because the alternative is worse than a refusal: a process
    that accepts the disagreement recomputes seat-scaled ceilings at a figure
    nobody chose, and every resulting ceiling looks entirely well-formed. A
    plausible value standing in for a failure is the one thing that cannot be
    detected afterwards, so the rate becomes a migration rather than a knob.
    """


#: Set this when the migration that recomputes every seat-tracked row is the
#: thing running. Nothing else may set it: it is the one door through which the
#: rate in force is allowed to change.
SEAT_RATE_MIGRATION_ENV = "STRATOCLAVE_SEAT_RATE_MIGRATION"

# The rate in force is one fact about the deployment, so it is stored once, on a
# control item, and read at boot. Each pool row ALSO carries the rate its own
# ceiling was computed at, because that is what makes the ceiling reproducible
# after the rate moves; the reconciler's `seat_rate_matches_rate_in_force` check
# is what keeps the two from drifting.
SEAT_RATE_CONTROL_PK = "__CONTROL__"
SEAT_RATE_CONTROL_SK = "SEAT_RATE"


def seat_rate_migration_allowed() -> bool:
    return str(os.getenv(SEAT_RATE_MIGRATION_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on")


def assert_seat_rate_in_force() -> Optional[int]:
    """Refuse to start when this process's configured seat rate disagrees with the
    rate the stored rows were computed at. Returns the rate in force, or None when
    nothing has recorded one yet.

    The rate is not a live knob. A process configured with a different figure does
    not gently start using it: every seat-scaled ceiling it then writes is computed
    at a number nobody chose, and each one looks entirely well-formed, so nothing
    afterwards can tell those ceilings from correct ones. Changing the rate is
    therefore a migration -- which recomputes every seat-tracked row and records the
    new rate -- and this refusal is what makes that the only path.

    Called from the boot path (`main.py`). A check no boot path calls is a check
    that passes in CI and is absent in production, which is the failure this
    deployment has already had once: a hardcoded value in the CDK made a knob inert
    exactly where it mattered.
    """
    configured = seat_rate_microusd()
    try:
        in_force = TenantBudgetsRepository().rate_in_force_microusd()
    except Exception as exc:  # noqa: BLE001
        # A read that FAILED is not evidence of a disagreement, and refusing on it
        # would turn a transient DynamoDB error -- or a process that legitimately has
        # no table in front of it -- into a boot failure across the fleet. This check
        # exists to catch a MISMATCH, so only a read that succeeds and disagrees
        # refuses.
        #
        # The trade, stated because it is real: a task that boots without confirming
        # the rate may write seat-scaled ceilings at a rate the fleet has moved off.
        # The reconciler's `seat_rate_matches_rate_in_force` check finds those rows a
        # day later, which is the same lateness the rest of that reconciler already
        # accepts, and it is a far smaller failure than refusing every deploy whose
        # first DynamoDB call happens to fail.
        from core.logging import get_logger

        get_logger(__name__).warning(
            "seat_rate_in_force_unreadable", error=str(exc),
            configured_microusd=configured)
        return None
    if in_force is None or in_force == configured:
        return in_force
    if seat_rate_migration_allowed():
        return in_force
    raise SeatRateMismatchError(
        f"STRATOCLAVE_SEAT_MONTHLY_USD is ${seat_monthly_usd()} "
        f"({configured} micro-USD/seat/month) but the rate in force on the stored "
        f"pool rows is {in_force} micro-USD/seat/month. The rate is not a live "
        f"knob: starting with this disagreement would recompute seat-scaled "
        f"ceilings at a figure nobody chose. Either restore the configured value, "
        f"or run `python -m migrations.pool_ceiling_migration --recompute-seat-rate "
        f"--apply` with {SEAT_RATE_MIGRATION_ENV}=1 set, which recomputes every "
        f"seat-tracked row and records the new rate."
    )


class PoolLimitExceedsMaximumError(ValueError):
    """Raised when seats x SEAT_MONTHLY_USD would exceed MAX_POOL_BUDGET_USD_CENTS
    (L8). Validated together at the creation path so a seat-scaled figure can
    never silently clamp to a smaller number than the seats imply -- a tenant
    whose seat count would exceed the maximum is refused loudly instead."""


def seat_pool_limit_microusd(seats: int) -> int:
    """`seats x SEAT_MONTHLY_USD`, in integer micro-USD.

    Raises `PoolLimitExceedsMaximumError` if that figure exceeds
    `MAX_POOL_BUDGET_USD_CENTS` (L8) -- the one place seats and the pool
    maximum are validated together, so a caller cannot construct a ceiling the
    pool item would then silently clamp.
    """
    from limits import MAX_POOL_BUDGET_USD_CENTS  # local import: dynamo/ does not
    # otherwise depend on the API-layer validation module (see limits.py's own
    # docstring for why it lives outside dynamo/).

    seats_int = int(seats)
    if seats_int < 0:
        raise ValueError(f"seats must be >= 0, got {seats_int}")
    rate = seat_monthly_usd()
    limit_microusd = seats_int * rate * 1_000_000
    max_microusd = int(MAX_POOL_BUDGET_USD_CENTS) * 10_000  # 1 cent = 10_000 microUSD
    if limit_microusd > max_microusd:
        raise PoolLimitExceedsMaximumError(
            f"{seats_int} seats x ${rate}/seat/mo = {limit_microusd} microUSD, "
            f"which exceeds MAX_POOL_BUDGET_USD_CENTS={MAX_POOL_BUDGET_USD_CENTS}"
        )
    return limit_microusd

# The operator set is a conditional CAS on the ceiling (Fable review finding 3).
# Concurrent admin writes to the SAME period's ceiling are rare, so a small
# bounded retry is plenty; exceeding it is a genuine anomaly worth surfacing.
_SET_LIMIT_MAX_RETRIES = 8

# A low-level (typed-value) DynamoDB client for the marker credit-back
# TransactWriteItems. Constructed off the plain client, not the resource's
# `.meta.client`, so the transact fragments' DynamoDB-JSON typed values pass
# through untouched. Cached per process.
_BUDGETS_LL_CLIENT = None


def _budgets_low_level_client():
    global _BUDGETS_LL_CLIENT
    if _BUDGETS_LL_CLIENT is None:
        import os

        import boto3

        from core.aws_pool import boto_config

        from .client import DYNAMODB_POOL_ENV
        _BUDGETS_LL_CLIENT = boto3.client(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=boto_config(DYNAMODB_POOL_ENV))
    return _BUDGETS_LL_CLIENT


def _reset_budgets_low_level_client() -> None:
    """Test hook: drop the cached low-level client so a new moto region takes
    effect (mirrors mvp._pipeline._reset_low_level_client)."""
    global _BUDGETS_LL_CLIENT
    _BUDGETS_LL_CLIENT = None


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


# PENDING protocol marker item (docs/design/pending-protocol.md, PR-1). The
# marker is the OBSERVABLE proof that a hold's pool debit committed, and the
# idempotency anchor for the non-transactional reserve. The measured
# marker-in-the-pool-item design (a per-hold entry in an `applied` MAP on the one
# hot pool item) was REJECTED: DynamoDB write WCU is proportional to item size, so
# the unbounded map bloated the single pool item and every debit's cost rose
# super-linearly under load (bench/ledger-latency/bench_marker_shard_spike.py). The
# corrected design puts each marker in its OWN fixed-size item, so its size — and
# thus its write cost — is O(1) regardless of how many holds a tenant has. It still
# shares the tenant partition (SK-scoped), which kills the growth blowup; the
# single-partition WCU ceiling remains bounded by the pool item itself and is a
# separate concern deferred to a sharded-pool PR.
def marker_sk(hold_id: str) -> str:
    """SK of a hold's separate marker item: ``MARKER#<hold_id>``. Keyed under the
    tenant partition (same PK=tenant_id) but on its own item, so writing/reading a
    marker never touches the pool item and never grows it."""
    return f"MARKER#{hold_id}"


# Marker lifecycle phases (Fable PR-1 review, Q2). The phase — NOT mere presence —
# is the exactly-once credit-back arbiter: a credit-back is a phase CAS
# RESERVED -> SETTLED, so a second credit of the same hold fails the CAS and cannot
# double-return headroom. Presence alone would let a settle that keeps the marker
# (for retry-dedup) be credited twice.
MARKER_RESERVED = "RESERVED"   # debit committed, headroom still held out
MARKER_SETTLED = "SETTLED"     # headroom returned exactly once; marker awaits TTL GC

# All marker/terminal cleanup timers derive from ONE shared window (Fable PR-1
# Q2/Q4-item-4): the marker must outlive every possible retry of its reserve so a
# late retry cannot pass `attribute_not_exists` and double-debit, AND outlive the
# reconcile window so a leak recovery is never GC'd before it runs. reconcile
# window + a 7-day margin. DynamoDB TTL only ever deletes LATE, never early, so
# this is a safe lower bound. Stamped ONLY at a terminal transition (settle / void
# / reconcile), NEVER at marker creation (an active hold's marker must never
# expire and reopen the double-debit window).
_RECONCILE_WINDOW_SECONDS = 24 * 60 * 60          # 1 day
_MARKER_TTL_MARGIN_SECONDS = 7 * 24 * 60 * 60      # 7 days
_MARKER_TTL_SECONDS = _RECONCILE_WINDOW_SECONDS + _MARKER_TTL_MARGIN_SECONDS


def _marker_ttl_epoch(now_epoch: Optional[int] = None) -> int:
    """Absolute epoch at which a SETTLED marker becomes TTL-eligible."""
    import time as _time

    base = int(now_epoch) if now_epoch is not None else int(_time.time())
    return base + _MARKER_TTL_SECONDS


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

    @staticmethod
    def _estimate_item_size_bytes(item: dict[str, Any]) -> int:
        """Approximate a DynamoDB item's stored size in bytes (attribute-name bytes
        + value bytes), the quantity WCU is charged on. Used by the PENDING-protocol
        pool item-size metric (docs/design/pending-protocol.md, PR-1 canary item A′):
        the WHOLE POINT of moving the marker out of the pool item is that the pool
        item stays SMALL and FLAT — this lets an alarm fire the instant a code
        regression reintroduces growth on the hot item. Rough by design (numbers are
        counted as their UTF-8 string length, matching DynamoDB's own ~ accounting);
        a monitoring signal, never a money quantity."""
        def _val_bytes(v: Any) -> int:
            if isinstance(v, dict):
                return sum(len(str(k)) + _val_bytes(vv) for k, vv in v.items())
            if isinstance(v, (list, tuple, set)):
                return sum(_val_bytes(x) for x in v)
            if isinstance(v, bool):
                return 1
            return len(str(v))
        return sum(len(str(name)) + _val_bytes(val) for name, val in (item or {}).items())

    def pool_item_size_bytes(self, tenant_id: str, period: str) -> Optional[int]:
        """Estimated stored size of the pool item (or None if absent). The canary
        detector for the item-growth regression the separate-item marker fixed: a
        healthy pool item holds exactly the attributes `pool_row_schema` declares and
        MUST NOT grow with the number of holds. The bound to compare against is
        `pool_row_schema.worst_case_pool_item_bytes()`, derived from that declaration,
        so it moves with a schema change instead of going stale; emit both as a gauge.
        Eventually-consistent read (Fable E-phase review Q2): a monitoring gauge does
        not need the current instant — an eventually-consistent GetItem halves RCU
        and loses nothing for this signal."""
        item = self.get(tenant_id, period, consistent_read=False)
        return None if item is None else self._estimate_item_size_bytes(item)

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

    # ----- the seat rate in force -----
    def rate_in_force_microusd(self) -> Optional[int]:
        """The per-seat rate the stored rows were computed at, or None if nothing
        has recorded one yet (a deployment M1 has not run against).

        Stored on ONE control item rather than inferred from a sample of rows,
        because a boot check cannot scan a fleet and a sampled answer would make
        the refusal depend on which row it happened to read. Each pool row still
        carries its OWN rate -- that is what makes an individual ceiling
        reproducible -- and the reconciler's `seat_rate_matches_rate_in_force`
        check is what stops the two from drifting apart.
        """
        resp = self._table.get_item(
            Key={"tenant_id": SEAT_RATE_CONTROL_PK, "sk": SEAT_RATE_CONTROL_SK},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        v = item.get(SEAT_RATE_ATTR)
        return None if v is None else int(v)

    def record_rate_in_force(self, *, rate_microusd: int) -> None:
        """Record the rate in force. The migration is the only caller: this is the
        one door through which the rate is allowed to change, which is what makes
        the boot-time refusal below honest rather than an obstacle."""
        self._table.put_item(Item={
            "tenant_id": SEAT_RATE_CONTROL_PK,
            "sk": SEAT_RATE_CONTROL_SK,
            SEAT_RATE_ATTR: Decimal(int(rate_microusd)),
            "updated_at": _now_iso(),
        })

    def previously_pooled(self, tenant_id: str, period: str) -> bool:
        """Did this tenant hold a pool row BEFORE `period`?

        The fact that separates the two meanings of a missing row, and it is a named
        method rather than an inline read because those two meanings are not obviously
        different at a call site. An absent row has always meant "this tenant is not
        pooled", which is right -- pool budgeting is opt-in. Once the row is
        per-period, that reading acquires a second case: a tenant that IS pooled whose
        row for this period has not been created yet. Reading the second as opt-out
        spends the month with no money ceiling at all.

        True means the row is MISSING and the caller must refuse rather than admit.
        False means the tenant is genuinely unpooled and nothing has changed for it.

        Eventually consistent, and that is deliberate on both counts. It answers a
        question about a CLOSED period, whose row is not being written any more, so
        there is nothing for a strong read to be more current about; and it is reached
        only on the miss path, which is already cold, so it must not cost the extra
        latency of a consistent read on the way to a refusal.
        """
        return self.get(tenant_id, previous_period(period)) is not None

    def pool_summary(self, tenant_id: str, period: str) -> Optional[dict[str, Any]]:
        """The pool's ceiling, its composition and its live usage in micro-USD,
        or None when the tenant has no pool budget for the period.

        Carries the whole ceiling composition rather than the one total, because
        the total on its own cannot be checked: an admin looking at a figure has
        no way to tell a seat-tracked row from an operator's own, and no way to
        see that an entitlement has outgrown a figure someone set months ago.
        `pool_granted_microusd` is reported as a plain zero until grants exist,
        so the composition printed beside the limit always adds up to it.

        `remaining_microusd` is SIGNED and never clamped. A row whose ceiling was
        lowered below its committed spend has negative headroom, and that deficit
        is the figure an operator has to act on -- clamping it at zero reports
        "nothing left" for both "exactly nothing left" and "already $400 over",
        which are different problems. There is deliberately no second name for it:
        one source of a fact gets one name, and a synonym is a second place a
        reader has to check for agreement.

        `over_ceiling_microusd` is a DIFFERENT fact rather than a restatement -- the
        magnitude of the overshoot, zero whenever there is none -- so a surface can
        ask "is this row over, and by how much" without inspecting a sign.
        """
        item = self.get(tenant_id, period)
        if not item:
            return None
        limit = int(item.get("pool_limit_microusd", 0))
        reserved = int(item.get("pool_reserved_microusd", 0))
        settled = int(item.get("pool_settled_microusd", 0))
        # Reported from the authoritative headroom counter when it exists (a row
        # written/backfilled under the new scheme), else derived from the mirrors
        # (a legacy row not yet backfilled). They are equal by the maintained
        # invariant; preferring headroom keeps the read consistent with the gate
        # the reserve actually checks.
        headroom = int(item["pool_headroom_microusd"]) \
            if "pool_headroom_microusd" in item else limit - reserved - settled
        seat_count = int(item.get(SEAT_COUNT_ATTR, 0))
        rate = row_seat_rate_microusd(item)
        manual = None if is_seat_tracked(item) else int(item[MANUAL_LIMIT_ATTR])
        return {
            "pool_limit_microusd": limit,
            "pool_reserved_microusd": reserved,
            "pool_settled_microusd": settled,
            "pool_headroom_microusd": headroom,
            # Signed, and the one the surfaces render. One name, not two.
            "remaining_microusd": headroom,
            "over_ceiling_microusd": -headroom if headroom < 0 else 0,
            "status": item.get("status", "active"),
            # The composition. Absence of the operator's figure IS the mode, so
            # `manual_limit_microusd` is None exactly when the row follows seats.
            "seat_count": seat_count,
            "seat_rate_microusd": rate,
            "seat_entitlement_microusd": seat_count * rate,
            "manual_limit_microusd": manual,
            "seat_tracked": manual is None,
            "pool_granted_microusd": granted_microusd(item),
            "baseline_microusd": baseline_microusd(item),
            # The cap, with its absent-default made EXPLICIT rather than left for
            # a reader to infer. Three fields because there are three facts and
            # collapsing them loses the one that matters: `grant_cap_microusd` is
            # None exactly when nobody set a figure, `effective_...` is the number
            # in force either way, and `cap_is_derived` says which of those two a
            # surface is looking at. Without the third, a console showing "cap:
            # $1,000" cannot tell an operator whether that number will move when
            # the tenant hires.
            "grant_cap_microusd": grant_cap_microusd(item),
            "effective_grant_cap_microusd": effective_grant_cap_for_row(item),
            "grant_cap_is_derived": grant_cap_microusd(item) is None,
            "remaining_grant_cap_microusd": max(
                0, effective_grant_cap_for_row(item) - granted_microusd(item)),
        }

    # ----- write: the two ceiling writers -----
    # Exactly two things move this row's ceiling, and each one is a single
    # conditional write:
    #
    #   * a MEMBERSHIP change moves `seat_count` always, and the money only when
    #     the row is seat-tracked. It is a delta, so it composes with a live
    #     reserve instead of racing it.
    #   * an OPERATOR SET writes the figure and shifts the money by the BASELINE
    #     delta, under a CAS on the three values that delta was computed from.
    #
    # Every write moves `pool_headroom` by exactly the same amount it moves
    # `pool_limit`, which is what keeps `headroom == limit - reserved - settled`
    # true without ever recomputing headroom from the mirrors.

    def _seed_pool_row(
        self,
        *,
        tenant_id: str,
        period: str,
        manual_limit_microusd: Optional[int],
        status: str = "active",
        seat_count: int = 0,
        seat_rate: Optional[int] = None,
        carried: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Create the period's pool row, iff nobody else just created it.

        The ONE place a pool row comes into existence, so the row's shape is
        stated once: the three counters at zero, the stored seat rate, the seat
        count, and the operator's figure ONLY when there is one.
        `manual_limit_microusd=None` seeds a seat-tracked row -- by leaving the
        attribute OFF, which is the sentinel, rather than by writing a zero that
        would mean "refuse every request".

        `carried` is how the ROLLOVER hands over an attribute this signature has
        no parameter for, and it exists because the alternative was a silent
        failure with the declaration's own name on it. The rollover collects the
        attributes the declaration classifies as CARRIED and calls this; every
        carried attribute that also happened to be a parameter here arrived, and
        any that did not was collected and then dropped on the floor. That was
        true of nothing until the aggregate grant cap was classified as carried,
        at which point an explicitly-set cap would have evaporated on the 1st --
        the exact failure the closed-world declaration was built to make
        impossible, arriving through the consumer that reads it. Values in
        `carried` never override an attribute this method computes, so it cannot
        become a second way to set the ceiling.

        Returns True if this call created the row, False if it lost the race.
        """
        rate = int(seat_rate) if seat_rate is not None else seat_rate_microusd()
        baseline = (int(manual_limit_microusd) if manual_limit_microusd is not None
                    else int(seat_count) * rate)
        # `pool_granted` is absent on a fresh row and absence reads as zero, so
        # the identity `limit = baseline + coalesce(granted, 0)` holds here with
        # no granted term written.
        item: dict[str, Any] = {
            "tenant_id": tenant_id,
            "sk": budget_sk(period),
            "pool_limit_microusd": Decimal(baseline),
            "pool_headroom_microusd": Decimal(baseline),
            "pool_reserved_microusd": Decimal(0),
            "pool_settled_microusd": Decimal(0),
            SEAT_COUNT_ATTR: Decimal(int(seat_count)),
            SEAT_RATE_ATTR: Decimal(rate),
            "status": status,
            "version": "3",
            "updated_at": _now_iso(),
        }
        if manual_limit_microusd is not None:
            item[MANUAL_LIMIT_ATTR] = Decimal(int(manual_limit_microusd))
        for name, value in (carried or {}).items():
            if name not in item:
                item[name] = value
        try:
            self._table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(tenant_id)")
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def create_seat_tracked_pool(
        self, *, tenant_id: str, period: str, seat_count: int = 0,
        status: str = "active",
    ) -> dict[str, Any]:
        """Provision a tenant's pool for a period as SEAT-TRACKED.

        Called at tenant creation. Writes no operator figure at all, so the row
        follows the seat count from its first moment and reaches
        `seats x rate` through the same delta every later membership change
        applies -- rather than through a figure computed once at creation, which
        is right only until the first hire.
        """
        self._seed_pool_row(
            tenant_id=tenant_id, period=period, manual_limit_microusd=None,
            status=status, seat_count=seat_count)
        return self.get(tenant_id, period) or {}

    def set_manual_limit(
        self,
        *,
        tenant_id: str,
        period: str,
        manual_limit_microusd: int,
        status: str = "active",
    ) -> dict[str, Any]:
        """Set the operator's own ceiling figure for a period, latching the row
        off seat tracking.

        `manual_limit_microusd` is a figure, and `0` is a legal one meaning every
        request refused. Asking for seat tracking back is `clear_manual_limit`,
        which REMOVES the attribute -- there is no in-band value that means
        "follow the seats", deliberately, because zero already means something
        else to every existing caller.

        RACE-SAFETY: `SET manual_limit = :asked ADD pool_limit :delta,
        pool_headroom :delta` where `delta = new_baseline - old_baseline`, under a
        CAS on all three values the delta was computed from (`seat_count`, the
        prior `manual_limit` including its absence, and `pool_limit`). The money
        moves as an ADD so a concurrent reserve's own `ADD pool_headroom :neg`
        composes with it rather than being clobbered; the CAS is on the inputs so
        a membership change that landed between the read and the write makes this
        retry rather than apply a delta computed from a stale seat count. A
        `ConditionalCheckFailed` means one of the three moved under us.

        The granted term is deliberately NOT in the delta: an operator's figure
        moves the baseline, and `pool_limit = baseline + granted` then moves by
        the same amount with the granted term untouched.
        """
        asked = int(manual_limit_microusd)
        for _attempt in range(_SET_LIMIT_MAX_RETRIES):
            existing = self.get(tenant_id, period, consistent_read=True)
            if existing is None:
                if self._seed_pool_row(
                        tenant_id=tenant_id, period=period,
                        manual_limit_microusd=asked, status=status):
                    return self.get(tenant_id, period) or {}
                continue  # someone created it first -> re-read and take the CAS

            if "pool_headroom_microusd" not in existing:
                # A row from before the headroom counter existed. Repair it to the
                # invariant first, race-safely, then take the ordinary path: this
                # branch is a migration artifact and must not also carry the
                # ceiling logic (it did once, and that duplication is where the
                # two readings of "headroom" came from).
                self.reconcile_headroom(tenant_id, period)
                continue

            old_baseline = baseline_microusd(existing)
            delta = asked - old_baseline
            old_limit = int(existing.get("pool_limit_microusd", 0))
            old_seats = int(existing.get(SEAT_COUNT_ATTR, 0))
            had_manual = not is_seat_tracked(existing)

            cond = ["pool_limit_microusd = :old_limit"]
            values: dict[str, Any] = {
                ":asked": Decimal(asked),
                ":delta": Decimal(delta),
                ":old_limit": Decimal(old_limit),
                ":status": status,
                ":ver": "3",
                ":now": _now_iso(),
            }
            if SEAT_COUNT_ATTR in existing:
                cond.append("seat_count = :old_seats")
                values[":old_seats"] = Decimal(old_seats)
            else:
                cond.append("attribute_not_exists(seat_count)")
            if had_manual:
                cond.append("manual_limit_microusd = :old_manual")
                values[":old_manual"] = Decimal(int(existing[MANUAL_LIMIT_ATTR]))
            else:
                cond.append("attribute_not_exists(manual_limit_microusd)")
            try:
                self._table.update_item(
                    Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                    # Literal attribute names, never assembled: the
                    # write-discipline guard reads these expressions statically, and
                    # a name it cannot resolve is a pool write it cannot track.
                    UpdateExpression=(
                        "SET manual_limit_microusd = :asked, #st = :status, "
                        "version = :ver, updated_at = :now "
                        "ADD pool_limit_microusd :delta, pool_headroom_microusd :delta"
                    ),
                    ConditionExpression=" AND ".join(cond),
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues=values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue  # an input moved under us -> re-read and recompute
                raise
            return self.get(tenant_id, period) or {}

        raise RuntimeError(
            f"set_manual_limit: lost the ceiling CAS {_SET_LIMIT_MAX_RETRIES}x for "
            f"{tenant_id}/{period}; concurrent writes to the same pool ceiling")

    def clear_manual_limit(self, *, tenant_id: str, period: str) -> dict[str, Any]:
        """Return the row to seat tracking: REMOVE the operator's figure and move
        the money to the seat term.

        This is the reversal the one-way door lacked. It is not "set the figure to
        the seat term" -- that would leave a figure behind, and the next hire
        would not move it. Absence is the state, so absence is what is written.

        Idempotent: a row that already follows seats has `delta = 0` and this is a
        touch. Same CAS shape as `set_manual_limit`, for the same reason.
        """
        for _attempt in range(_SET_LIMIT_MAX_RETRIES):
            existing = self.get(tenant_id, period, consistent_read=True)
            if existing is None:
                return {}
            if "pool_headroom_microusd" not in existing:
                self.reconcile_headroom(tenant_id, period)
                continue
            old_baseline = baseline_microusd(existing)
            new_baseline = seat_term_microusd(existing)
            delta = new_baseline - old_baseline
            old_limit = int(existing.get("pool_limit_microusd", 0))
            old_seats = int(existing.get(SEAT_COUNT_ATTR, 0))

            cond = ["pool_limit_microusd = :old_limit"]
            values: dict[str, Any] = {
                ":delta": Decimal(delta),
                ":old_limit": Decimal(old_limit),
                ":ver": "3",
                ":now": _now_iso(),
            }
            if SEAT_COUNT_ATTR in existing:
                cond.append("seat_count = :old_seats")
                values[":old_seats"] = Decimal(old_seats)
            else:
                cond.append("attribute_not_exists(seat_count)")
            if is_seat_tracked(existing):
                cond.append("attribute_not_exists(manual_limit_microusd)")
            else:
                cond.append("manual_limit_microusd = :old_manual")
                values[":old_manual"] = Decimal(int(existing[MANUAL_LIMIT_ATTR]))
            try:
                self._table.update_item(
                    Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                    UpdateExpression=(
                        "REMOVE manual_limit_microusd "
                        "SET version = :ver, updated_at = :now "
                        "ADD pool_limit_microusd :delta, pool_headroom_microusd :delta"
                    ),
                    ConditionExpression=" AND ".join(cond),
                    ExpressionAttributeValues=values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    continue
                raise
            return self.get(tenant_id, period) or {}

        raise RuntimeError(
            f"clear_manual_limit: lost the ceiling CAS {_SET_LIMIT_MAX_RETRIES}x for "
            f"{tenant_id}/{period}; concurrent writes to the same pool ceiling")

    def adjust_pool_for_seat_delta(
        self, *, tenant_id: str, period: str, seat_delta: int
    ) -> bool:
        """Apply a membership change to a tenant's pool row for `period`.

        `seat_count` moves on EVERY row, seat-tracked or not, and the money moves
        only when the row is seat-tracked. That split is the point of the change:
        the seat count is a fact about the tenant, so a manual row that stops
        counting seats stops being able to say its entitlement has outgrown its
        figure -- which is the one thing an operator needs in order to know the
        figure is now the wrong one.

        The seat-tracked write is `ADD seat_count :ds, pool_limit :d,
        pool_headroom :d` guarded by `attribute_not_exists(manual_limit)`. It is a
        PURE ADD by design: a delta needs no snapshot to be safe, so it composes
        with a live reserve's `ADD pool_headroom :neg` and with another membership
        delta, and there is no read-then-write window in which a concurrent move
        is lost. On a condition failure (the row carries an operator's figure) it
        RETRIES with `seat_count` alone, so the seat count is recorded either way.

        Returns True iff the money moved. Never raises for the row's state: a
        membership write must never fail, or be forced to retry, because of the
        pool's unrelated state, so a missing row is a no-op.

        A missing row is a no-op and NOT a create: `ADD` on an absent item creates
        it, and an item created by a membership delta would be a pool row with a
        ceiling and no seat rate, no status and no counters -- exactly the partial
        row a period boundary must not produce. Both writes are therefore
        conditioned on `attribute_exists(tenant_id)` as well.
        """
        if seat_delta == 0:
            return False
        # The rate the ROW's ceiling is denominated in, not the process's. They
        # agree on any booted process (R20's refusal is what makes that true), and
        # reading the row's own figure is what keeps a rate change a migration.
        existing = self.get(tenant_id, period, consistent_read=True)
        if existing is None:
            return False
        delta_microusd = int(seat_delta) * row_seat_rate_microusd(existing)
        seats = Decimal(int(seat_delta))
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                UpdateExpression=(
                    "ADD seat_count :ds, pool_limit_microusd :d, "
                    "pool_headroom_microusd :d SET updated_at = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(tenant_id) AND "
                    "attribute_not_exists(manual_limit_microusd)"
                ),
                ExpressionAttributeValues={
                    ":ds": seats,
                    ":d": Decimal(delta_microusd),
                    ":now": _now_iso(),
                },
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        # The row carries an operator's figure (or vanished). The money must not
        # move, but the seat count still must: retry with the seat count alone.
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
                UpdateExpression="ADD seat_count :ds SET updated_at = :now",
                ConditionExpression="attribute_exists(tenant_id)",
                ExpressionAttributeValues={":ds": seats, ":now": _now_iso()},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return False

    # ----- write: the grant writers -----
    # A grant moves the ceiling WITHOUT touching the baseline, and that is why
    # these two are pure `ADD`s while every writer above them takes a CAS. The
    # writers above compute a BASELINE delta from values that can move under
    # them, so they have to check those values are still what they read. A grant
    # is +G or -G on top of whatever baseline is in force at the instant it
    # commits, so it composes with a concurrent hire, a concurrent operator set
    # and a concurrent reserve rather than racing any of them. There is nothing
    # for a CAS to protect.
    #
    # They live HERE, on the repository that owns this row, rather than on the
    # repository that owns the grant record. The declaration in
    # `pool_row_schema` names the writers of this row, and a writer in another
    # module is a writer that declaration cannot see -- which is the second
    # authority over the row's shape that the declaration exists to prevent.

    def grant_apply_txn_item(
        self, *, target_pk: str, target_sk: str, approved_amount_microusd: int,
        cap_minus_amount: int,
    ) -> dict[str, Any]:
        """Transaction fragment applying a grant of `approved_amount_microusd`.

        Moves all three attributes by the SAME amount: the granted term, the
        ceiling, and the headroom. Moving the ceiling without the headroom would
        raise a limit the admission gate never sees, since the gate reads headroom
        alone.

        THE CAP GUARD DOES NOT MENTION THE CAP, and that is the shape B1 requires
        rather than an omission. An absent cap means "derived from the baseline",
        so a condition referencing `grant_cap_microusd` would fail outright on
        every row that has never had one set -- which is every row. The caller
        resolves the cap and passes `cap_minus_amount`; the condition then compares
        the row's LIVE granted sum against that literal, so a concurrent approval
        that already moved it is still caught at commit.

        The `attribute_not_exists` half of that condition is load-bearing and is
        the easy thing to get wrong. `pool_granted_microusd` is RESET BY OMISSION
        at every period boundary, so on the first grant of any period the
        attribute is absent -- and a DynamoDB comparison against a missing
        attribute FAILS. A guard written as `pool_granted_microusd <= :cap` alone
        would therefore refuse the first grant of every month on every tenant,
        reporting it as the cap being exceeded when nothing had been granted at
        all.
        """
        amount = int(approved_amount_microusd)
        return {
            "Update": {
                "TableName": self._name,
                "Key": {"tenant_id": {"S": target_pk}, "sk": {"S": target_sk}},
                "UpdateExpression": (
                    "ADD pool_granted_microusd :g, pool_limit_microusd :g, "
                    "pool_headroom_microusd :g SET updated_at = :now"
                ),
                "ConditionExpression": (
                    "attribute_exists(pool_limit_microusd) AND "
                    "(attribute_not_exists(pool_granted_microusd) OR "
                    "pool_granted_microusd <= :cap_minus_g)"
                ),
                "ExpressionAttributeValues": {
                    ":g": {"N": str(amount)},
                    ":cap_minus_g": {"N": str(int(cap_minus_amount))},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    def grant_revoke_txn_item(
        self, *, target_pk: str, target_sk: str, approved_amount_microusd: int,
    ) -> dict[str, Any]:
        """Transaction fragment giving a grant's capacity back.

        The exact reverse of the apply, on the row the grant was PINNED to rather
        than on the current period's -- the caller passes the keys the grant row
        carries, so a grant approved in July and revoked in August moves July's
        row.

        `pool_granted_microusd >= :g` is the floor. Without it a revoke of a grant
        whose apply never landed -- or a second revoke that somehow passed the
        grant row's own condition -- would drive the granted term negative and,
        with it, the ceiling and the headroom: a tenant refused below its own
        baseline, with every equation over the row still balancing.
        """
        amount = int(approved_amount_microusd)
        return {
            "Update": {
                "TableName": self._name,
                "Key": {"tenant_id": {"S": target_pk}, "sk": {"S": target_sk}},
                "UpdateExpression": (
                    "ADD pool_granted_microusd :neg, pool_limit_microusd :neg, "
                    "pool_headroom_microusd :neg SET updated_at = :now"
                ),
                "ConditionExpression": (
                    "attribute_exists(pool_limit_microusd) AND "
                    "pool_granted_microusd >= :g"
                ),
                "ExpressionAttributeValues": {
                    ":g": {"N": str(amount)},
                    ":neg": {"N": str(-amount)},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    def get_by_key(
        self, target_pk: str, target_sk: str, *, consistent_read: bool = True
    ) -> Optional[dict[str, Any]]:
        """Read a pool row by the exact key a grant was pinned to.

        The orphan hunt's primitive. `get(tenant_id, period)` would work only by
        reconstructing the sort key from the period, which is the recomputation
        the pinning exists to avoid: a grant records the keys it raised, and
        reading them back is what makes "does this row still exist" a question
        about the same row the revoke would move.
        """
        resp = self._table.get_item(
            Key={"tenant_id": target_pk, "sk": target_sk},
            ConsistentRead=consistent_read)
        return resp.get("Item")

    # ----- period rollover -----
    # R16's named owner. Nothing rolled a period over before this: a new month
    # simply had no row, and a membership change against a missing row was a
    # no-op, so a tenant's ceiling silently became "unlimited at the pool level"
    # on the 1st. The rollover is what makes the new period's row exist, and the
    # ONE thing it must not be is a hardcoded list of attributes to copy --
    # because the next part to add an attribute would have to remember to edit it,
    # and if it forgot, that attribute would evaporate every 1st with nothing
    # saying so. So it reads the closed-world declaration instead.

    def roll_period_forward(
        self, *, tenant_id: str, from_period: str, to_period: str
    ) -> Optional[dict[str, Any]]:
        """Create `to_period`'s pool row from `from_period`'s, carrying exactly
        the attributes the declaration classifies as carried and recomputing the
        derived ones. Returns the new row, or None when there is nothing to roll.

        Moves NO effective limit: a seat-tracked row arrives seat-tracked with the
        same seats and the same rate, so its ceiling is the same number; a manual
        row arrives with the same figure. What does NOT arrive is the spend, the
        reservations, or the granted term -- a new period starts at zero committed
        and, per the declaration, `pool_granted_microusd` is reset BY OMISSION so
        the first grant of the new period creates it.

        Idempotent, and it never overwrites: the seed is conditional on the new
        row not existing, so a second call (or a concurrent one) is a no-op and a
        row that has already taken traffic in the new period is never reset.
        """
        source = self.get(tenant_id, from_period, consistent_read=True)
        if source is None:
            return None
        seed: dict[str, Any] = {}
        for name in pool_row_schema.carried_attributes():
            if name in source:
                seed[name] = source[name]
        manual = seed.get(MANUAL_LIMIT_ATTR)
        created = self._seed_pool_row(
            tenant_id=tenant_id,
            period=to_period,
            # Carried by ABSENCE as well as by value: a seat-tracked row must
            # reach the new period still seat-tracked, and `None` here is what
            # leaves the attribute off.
            manual_limit_microusd=None if manual is None else int(manual),
            status=str(seed.get("status", "active")),
            seat_count=int(seed.get(SEAT_COUNT_ATTR, 0)),
            seat_rate=int(seed[SEAT_RATE_ATTR]) if SEAT_RATE_ATTR in seed else None,
            # Every carried attribute, including the ones this signature has no
            # parameter for. Reading the declaration and then handing only four of
            # its answers to the writer was a list in disguise: the declaration
            # said "carried" and the row it produced did not carry it.
            carried=seed,
        )
        new_row = self.get(tenant_id, to_period)
        if created and new_row is not None:
            # The declaration is only worth its cost if a consumer that ignores
            # part of it FAILS. Nothing else can notice this: a carried attribute
            # that did not arrive leaves a row that looks entirely well-formed,
            # and for the grant cap it looks like a DERIVED cap rather than like
            # an error. So the rollover checks its own work.
            lost = [name for name in pool_row_schema.carried_attributes()
                    if name in source and name not in new_row]
            if lost:
                raise RuntimeError(
                    f"roll_period_forward carried {sorted(lost)} out of "
                    f"{from_period} and the new {to_period} row does not have "
                    f"them. The declaration classifies them as carried, so this "
                    f"is the rollover failing to honour it -- not an optional "
                    f"attribute. Carry them in _seed_pool_row.")
        return new_row

    def ensure_current_period_row(
        self, *, tenant_id: str, period: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """The rollover as the request path meets it: if `period` has no row and
        the previous period does, roll it forward. Returns the row for `period`.

        Lazy rather than scheduled, because a scheduled job that misses a tenant
        leaves that tenant unlimited for a month and nothing about the request
        that was admitted says why. Called from the seat-delta path so a
        membership change on the 1st lands on a row rather than on nothing.
        """
        resolved = period or current_period()
        row = self.get(tenant_id, resolved, consistent_read=True)
        if row is not None:
            return row
        return self.roll_period_forward(
            tenant_id=tenant_id, from_period=previous_period(resolved),
            to_period=resolved)

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
        payload_bytes: Optional[int] = None,
        run_id: Optional[str] = None,
        run_id_is_fallback: bool = False,
        model_id: Optional[str] = None,
        reserved_tokens: Optional[int] = None,
        hold_user_id: Optional[str] = None,
        quota_period: Optional[str] = None,
        quota_amount: Optional[int] = None,
        quota_tenant_scope: Optional[bool] = None,
        quota_user_scope: Optional[bool] = None,
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
          * `payload_hash` — for an external authorize, the request fingerprint
            (a duplicate Idempotency-Key resolving to this hold 422s on a
            different body); for an INLINE hold (docs/design/hard-ceiling.md
            section 3a), the hash of the canonical outbound payload the
            reservation was bound against — pinned here, immutable for the
            life of the hold, so a retry can be verified byte-identical rather
            than merely trusted to be.
          * `payload_bytes` — the paired byte length for that same inline hash
            (contract section 7: recorded so the reservation is recomputable).
          * `model_id` — the canonical model this reservation was priced/quota'd
            against. Read back by the retained-hold admin path (display/ledger
            attribution) and by the per-model quota reversal below; a hold with
            no configured quota still benefits from carrying it for the former.
          * `reserved_tokens` / `hold_user_id` — the amount this hold's reserve
            added to the OWNER's `credit_used` (a UserTenants row, a different
            table keyed by user+tenant) and whose row it is. The reaper's reclaim
            and a released retention are the only readers: neither one is the
            request that made this reservation, so without these two facts frozen
            here neither can know what to give back on that counter — and it is
            not period-scoped and carries no TTL, so a leak there is permanent,
            unlike the pool amount this Put's sibling counters already recover.
          * `quota_period` / `quota_amount` / `quota_tenant_scope` /
            `quota_user_scope` — the per-model quota reservation (if any) this
            hold's reserve committed alongside the pool debit: the period and
            amount it moved, and which of the tenant-scope / user-scope `used`
            rows actually received it (`build_reserve_txn_items` writes one, the
            other, or both, depending on which limits are configured for
            `model_id` — a reversal that assumed both would be writing into
            a per-model quota row this hold never touched whenever only one
            scope was ever configured). `quota_tenant_scope`/`quota_user_scope`
            are only meaningful together with `quota_amount`; a caller with no
            quota reservation to record leaves all four unset.

        Inline holds pass `source="inline"` plus `payload_hash`/`payload_bytes`;
        external authorize passes the full legacy set. Absent args are simply
        not written (no None in DDB).
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
        if payload_bytes is not None:
            item["payload_bytes"] = {"N": str(int(payload_bytes))}
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
        if model_id:
            item["model_id"] = {"S": str(model_id)}
        if reserved_tokens is not None:
            item["reserved_tokens"] = {"N": str(int(reserved_tokens))}
        if hold_user_id:
            item["user_id"] = {"S": str(hold_user_id)}
        if quota_period:
            item["quota_period"] = {"S": str(quota_period)}
        if quota_amount is not None:
            item["quota_amount"] = {"N": str(int(quota_amount))}
        if quota_tenant_scope is not None:
            item["quota_tenant_scope"] = {"BOOL": bool(quota_tenant_scope)}
        if quota_user_scope is not None:
            item["quota_user_scope"] = {"BOOL": bool(quota_user_scope)}
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
        model_id: Optional[str] = None,
        reserved_tokens: Optional[int] = None,
        hold_user_id: Optional[str] = None,
        quota_period: Optional[str] = None,
        quota_amount: Optional[int] = None,
        quota_tenant_scope: Optional[bool] = None,
        quota_user_scope: Optional[bool] = None,
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
        auto-serialized) so it always binds to the same session as the repo.

        `model_id` / `reserved_tokens` / `hold_user_id` / `quota_period` /
        `quota_amount` / `quota_tenant_scope` / `quota_user_scope` carry the exact
        same facts `hold_put_txn_item` documents, so a hold reserved under
        `STRATOCLAVE_RESERVE_PROTOCOL=pending` is reclaimable by the same reaper
        logic as a transactional one — a caller that reserves per-user tokens or a
        per-model quota alongside a pending pool debit must not lose reclaim
        coverage a transactional reserve already has."""
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
        if model_id:
            item["model_id"] = str(model_id)
        if reserved_tokens is not None:
            item["reserved_tokens"] = int(reserved_tokens)
        if hold_user_id:
            item["user_id"] = str(hold_user_id)
        if quota_period:
            item["quota_period"] = str(quota_period)
        if quota_amount is not None:
            item["quota_amount"] = int(quota_amount)
        if quota_tenant_scope is not None:
            item["quota_tenant_scope"] = bool(quota_tenant_scope)
        if quota_user_scope is not None:
            item["quota_user_scope"] = bool(quota_user_scope)
        self._table.put_item(Item=item, ConditionExpression=Attr("sk").not_exists())

    # Sentinel returned by reserve_commit_transact to distinguish the three
    # outcomes of the pool-debit + marker-Put transaction.
    RESERVE_APPLIED = "applied"        # the debit committed on THIS call (200)
    RESERVE_ALREADY = "already"        # this hold's marker already present (idempotent)
    RESERVE_EXHAUSTED = "exhausted"    # genuine budget exhaustion (402)

    def reserve_commit_txn_items(self, *, tenant_id: str, period: str, hold_id: str,
                                 amount_microusd: int) -> list[dict[str, Any]]:
        """The two low-level TransactWriteItems fragments for the PENDING-protocol
        COMMIT POINT (docs/design/pending-protocol.md, PR-1):

          0. pool debit — ``ADD headroom :neg, reserved :amt`` guarded by
             ``headroom >= amount AND status = active`` (genuine-exhaustion gate).
          1. marker Put — a SEPARATE fixed-size item ``SK=MARKER#<hold_id>`` guarded
             by ``attribute_not_exists(sk)`` (the idempotency anchor).

        Composed into ONE TransactWriteItems so the debit and its observable proof
        are atomic. The marker item carries the amount (immutable once written — the
        exactly-once credit-back reads it) and ``marker_phase=RESERVED``. Returned as
        a list so the caller assigns positions and reads CancellationReasons by
        index. Order is a CONTRACT: index 0 = pool (pool-side CCF ⇒ 402), index 1 =
        marker (marker-side CCF ⇒ already applied ⇒ idempotent success)."""
        return [
            {
                "Update": {
                    "TableName": self._name,
                    "Key": {"tenant_id": {"S": tenant_id}, "sk": {"S": budget_sk(period)}},
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
            },
            {
                "Put": {
                    "TableName": self._name,
                    "Item": {
                        "tenant_id": {"S": tenant_id},
                        "sk": {"S": marker_sk(hold_id)},
                        "hold_id": {"S": hold_id},
                        "period": {"S": period},
                        "amount_microusd": {"N": str(int(amount_microusd))},
                        "marker_phase": {"S": MARKER_RESERVED},
                        "created_at": {"S": _now_iso()},
                    },
                    "ConditionExpression": "attribute_not_exists(sk)",
                }
            },
        ]

    def pool_marker_amount(self, *, tenant_id: str, period: str, hold_id: str) -> Optional[int]:
        """ConsistentRead of this hold's separate marker item's amount, or None if
        the marker is absent. The local, decisive answer to 'did this hold's debit
        commit?' (A1 restored without a transaction). Used by reserve replay + the
        capture helping path + the ambiguous-failure resolution. A SETTLED marker
        (awaiting TTL GC) still returns its amount — the debit DID commit — so the
        caller must NOT read this as "still outstanding"; use `marker_phase` for
        that. `period` is accepted for signature stability but the marker item is
        period-independent (keyed by hold_id, which is period-namespaced)."""
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": marker_sk(hold_id)}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return None
        v = item.get("amount_microusd")
        return int(v) if v is not None else None

    def marker_settle_best_effort(self, *, tenant_id: str, hold_id: str,
                                  now_epoch: Optional[int] = None) -> None:
        """Cleanup-only marker transition RESERVED -> SETTLED + TTL stamp, for the
        settle / release / reclaim paths (docs/design/pending-protocol.md, PR-1).
        Those paths already return the hold's headroom ATOMICALLY (pool item, in
        their own transaction) and DELETE/expire the hold, so the marker plays NO
        money role there — it only needs SETTLING so it (a) stops looking
        outstanding and (b) becomes TTL-eligible. Money-safety does NOT depend on
        this landing: exactly-once credit-back is enforced by `pool_credit_back`'s
        phase CAS, and a settled/reclaimed hold is deleted/EXPIRED (never
        EXPIRED_UNCREDITED), so the reconciler can never credit it. A marker this
        misses is a bounded STORAGE orphan the reconcile audit sweep will settle.
        Never raises; no-op if the marker is absent or already SETTLED."""
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": marker_sk(hold_id)},
                UpdateExpression="SET marker_phase = :settled, #ttl = :ttl, settled_at = :now",
                ConditionExpression="marker_phase = :reserved",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":settled": MARKER_SETTLED,
                    ":reserved": MARKER_RESERVED,
                    ":ttl": _marker_ttl_epoch(now_epoch),
                    ":now": _now_iso(),
                },
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return  # absent / already SETTLED — nothing to do
            # Any other error is swallowed: this is cleanup, never money-critical.
            return

    def list_reserved_markers(self, *, tenant_id: str, limit: int = 50,
                              max_pages: int = 20) -> list[dict[str, Any]]:
        """Bounded strongly-consistent scan of a tenant's RESERVED markers (the
        reconcile audit sweep's input — Fable PR-1 Q2 hole 3). Used to find markers
        orphaned by a settle/reclaim whose best-effort transition was lost, so they
        can be settled + TTL'd.

        PAGINATES (Fable PR-1 review, medium): DynamoDB's ``Limit`` bounds items
        EVALUATED, applied BEFORE the ``marker_phase = RESERVED`` filter. SETTLED
        markers linger up to the TTL window (~8 days), so a single page could be
        entirely SETTLED and hide RESERVED orphans behind them. We follow
        ``LastEvaluatedKey`` until ``limit`` RESERVED markers are collected or
        ``max_pages`` is reached (a cold-path safety bound; reconcile re-runs pick up
        any remainder next pass). Range-Queries the ``MARKER#`` SK prefix under the
        tenant partition."""
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("tenant_id").eq(tenant_id) & Key("sk").begins_with("MARKER#")
            ),
            "FilterExpression": Attr("marker_phase").eq(MARKER_RESERVED),
            "ConsistentRead": True,
        }
        for _ in range(int(max_pages)):
            resp = self._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if len(out) >= int(limit) or not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return out[: int(limit)]

    def hold_exists_by_id(self, *, tenant_id: str, period: str, hold_id: str) -> bool:
        """True iff a HOLD row of ANY status still exists for `hold_id` (Fable PR-1
        review condition 2). Strongly-consistent range-Query of the period's
        ``HOLD#`` prefix filtered to this hold_id — so the reconcile audit sweep can
        confirm a marker is a genuine post-terminal orphan WITHOUT depending on the
        completeness of a separate `list_holds` page. The hold's SK embeds the
        expiry (unknown to the marker), so an exact GetItem is impossible; this
        filtered Query is the cold-path equivalent. FULLY PAGINATES (a truncated
        first page would falsely report absence — DynamoDB's Limit bounds items
        evaluated BEFORE the FilterExpression), stopping as soon as the single
        possible match is found."""
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("tenant_id").eq(tenant_id) & Key("sk").begins_with(hold_sk_prefix(period))
            ),
            "FilterExpression": Attr("hold_id").eq(hold_id),
            "ConsistentRead": True,
        }
        while True:
            resp = self._table.query(**kwargs)
            if resp.get("Items"):
                return True
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return False
            kwargs["ExclusiveStartKey"] = lek

    def get_marker(self, *, tenant_id: str, hold_id: str) -> Optional[dict[str, Any]]:
        """Strongly-consistent read of a hold's full marker item (amount + phase),
        or None. `marker_phase == RESERVED` means the debit is committed AND its
        headroom is still held out; `SETTLED` means it was already credited back and
        the item is only alive for retry-dedup until TTL. Absent ⇒ no debit."""
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": marker_sk(hold_id)}, ConsistentRead=True)
        return resp.get("Item")

    def marker_credit_back_txn_item(self, *, tenant_id: str, hold_id: str,
                                    now_epoch: Optional[int] = None) -> dict[str, Any]:
        """TransactWriteItems fragment that flips a marker RESERVED -> SETTLED and
        stamps its TTL, guarded by ``marker_phase = RESERVED``. Paired IN THE SAME
        transaction with the pool credit-back (``headroom += amount``): the phase
        CAS is the exactly-once arbiter, so a second credit of the same hold fails
        this condition and the whole transaction cancels — no double-return of
        headroom. The marker is NOT deleted here (it must survive to dedupe a late
        reserve retry); TTL cleans it up after the window."""
        return {
            "Update": {
                "TableName": self._name,
                "Key": {"tenant_id": {"S": tenant_id}, "sk": {"S": marker_sk(hold_id)}},
                "UpdateExpression": "SET marker_phase = :settled, #ttl = :ttl, settled_at = :now",
                "ConditionExpression": "marker_phase = :reserved",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": {
                    ":settled": {"S": MARKER_SETTLED},
                    ":reserved": {"S": MARKER_RESERVED},
                    ":ttl": {"N": str(_marker_ttl_epoch(now_epoch))},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    def pool_credit_back(self, *, tenant_id: str, period: str, hold_id: str) -> bool:
        """Exactly-once credit-back for the PENDING protocol, now a two-item
        TransactWriteItems (Fable PR-1 Q2/Q4-item-3 — a lone UpdateItem is
        forbidden here: a hold-delete that succeeds while a separate credit-back
        UpdateItem fails would strand a RESERVED marker and leak headroom forever).
        Atomically:

          * pool: ``headroom += amount, reserved -= amount`` (amount read from the
            marker item, passed in via a pre-read so the counter move is exact);
          * marker: phase CAS RESERVED -> SETTLED + TTL stamp.

        The phase CAS is the arbiter: a second credit of the same hold cancels on
        the marker condition, so double-return is structurally impossible. Returns
        True if it credited on THIS call, False if the marker was absent or already
        SETTLED (already credited / never debited) — both leak-safe, never oversell.
        This is the ONLY way credit-back happens under the PENDING protocol."""
        marker = self.get_marker(tenant_id=tenant_id, hold_id=hold_id)
        if not marker or str(marker.get("marker_phase")) != MARKER_RESERVED:
            return False   # absent or already SETTLED — nothing to credit (leak-safe)
        # Defensive period cross-check (Fable PR-1 review non-blocking note): the
        # marker records the period its debit hit; hold_id is period-namespaced so a
        # mismatch should be impossible, but crediting the WRONG period's pool would
        # be silent corruption. Refuse rather than move money on inconsistent state.
        m_period = marker.get("period")
        if m_period is not None and str(m_period) != period:
            raise ValueError(
                f"pool_credit_back period mismatch: marker={m_period!r} arg={period!r} "
                f"for hold {hold_id}")
        amount = int(marker.get("amount_microusd", 0))
        items = [
            {
                "Update": {
                    "TableName": self._name,
                    "Key": {"tenant_id": {"S": tenant_id}, "sk": {"S": budget_sk(period)}},
                    "UpdateExpression": (
                        "ADD pool_headroom_microusd :amt, pool_reserved_microusd :neg "
                        "SET updated_at = :now"
                    ),
                    "ConditionExpression": "attribute_exists(tenant_id)",
                    "ExpressionAttributeValues": {
                        ":amt": {"N": str(amount)},
                        ":neg": {"N": str(-amount)},
                        ":now": {"S": _now_iso()},
                    },
                }
            },
            self.marker_credit_back_txn_item(tenant_id=tenant_id, hold_id=hold_id),
        ]
        try:
            _budgets_low_level_client().transact_write_items(TransactItems=items)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            # Distinguish WHY it cancelled (Fable PR-1 review Bug 2 — a blanket
            # `return False` conflates "already credited" with "nothing committed,
            # retry me", so a transient made the reconciler retire the hold and
            # strand the RESERVED marker = permanent leak). Items are [pool(0),
            # marker(1)]. Only a MARKER-side ConditionalCheckFailed means the phase
            # CAS lost (already SETTLED / absent) → definitively already credited →
            # False (leak-safe, caller may retire). Anything else — a pool-side
            # attribute_exists(tenant_id) failure (pool row vanished), a
            # TransactionConflict on the hot pool item, or throttling — committed
            # NOTHING and MUST be retried: raise so the reconciler leaves the hold
            # EXPIRED_UNCREDITED for the next pass instead of retiring it.
            reasons = [r.get("Code", "") for r in
                       (e.response.get("CancellationReasons", []) or [])]
            marker_ccf = len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed"
            if marker_ccf:
                return False
            raise

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

    def hold_mark_departed(self, *, tenant_id: str, sk: str, state: str) -> bool:
        """Record on the hold that a provider call left and its outcome was not seen.

        The hold is written before the call, so at that point nothing knows whether a
        call will depart. Only the ENDING knows, and only for the outcomes it could
        classify — which is why this is written there and not on the way out: it costs
        nothing on a request that completes normally.

        This is the fact a reclaim needs later. Without it the reclaim can only assume
        the call never happened, which is the assumption measured to be false; with it,
        the reclaim can decline to hand the budget back. Nothing else establishes it,
        and the retention that depends on it is unreachable if this write does not
        happen — so a caller must treat a False as the degradation it is, not as a
        detail.

        Touches `provider_invoked_at` and `unobserved_state` only. No aggregate is
        read or written here, which is why it is a single-item update rather than a
        transaction. Conditional on the row still existing: a hold already ended by
        someone else is not ours to annotate."""
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": sk},
                UpdateExpression=(
                    "SET provider_invoked_at = :now, unobserved_state = :state"),
                ConditionExpression="attribute_exists(sk)",
                ExpressionAttributeValues={":now": _now_iso(), ":state": str(state)},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def hold_retain(self, *, tenant_id: str, sk: str) -> bool:
        """ACTIVE (or the pre-PENDING implicit ACTIVE) -> RETAINED, pool UNTOUCHED.

        The reservation stays exactly where it is, on the pool's reserved counter.
        That is what makes this cheap and what makes it safe: retaining a reservation
        needs no new counter, no change to the admission arithmetic, and no change to
        the money model the proofs are over — the amount was already being counted
        against the limit, and it goes on being counted. This write touches only
        `status`, which is why it is a single-item update rather than a transaction:
        it moves no money, and the money it declines to move is money that is
        already, correctly, where it is.

        What changes is only that the reaper stops offering to give it back. Every
        sweep skips a hold whose status is not ACTIVE, so one conditional status
        write is the whole mechanism.

        Conditional on the row being ACTIVE or carrying no status at all, so it
        cannot retain a PENDING hold (whose debit may never have committed) and
        cannot race a settle that is deleting the row. Returns False when the
        condition failed, which the caller must treat as "someone else ended it",
        never as success."""
        try:
            self._table.update_item(
                Key={"tenant_id": tenant_id, "sk": sk},
                UpdateExpression="SET #st = :to, retained_at = :now",
                ConditionExpression=(
                    "attribute_exists(sk) AND "
                    "(attribute_not_exists(#st) OR #st = :active)"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":to": "RETAINED", ":active": "ACTIVE", ":now": _now_iso()},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def list_retained_holds(
        self, *, tenant_id: str, period: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Holds this tenant/period is holding budget for pending a resolution.

        A retained hold has no ending yet on purpose, so nothing else will surface
        it: an operator needs a list to act on, and a reconciliation needs a figure
        to explain why `reserved` is not going down. Bounded and strongly
        consistent; the caller is an admin read, not the request path."""
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("tenant_id").eq(tenant_id)
                & Key("sk").begins_with(hold_sk_prefix(period))
            ),
            "FilterExpression": Attr("status").eq("RETAINED"),
            "ConsistentRead": True,
        }
        while len(out) < limit:
            resp = self._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return out[:limit]

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
        default) the Delete is gated on the SAME two facts `reclaim_hold_txn_item`
        gates on, so the settle side and the reaper cannot both believe they are the
        one ending this reservation:

          * `attribute_exists(sk)` — the latch that keeps the paired aggregate
            decrement from applying twice. If the reaper already reclaimed this hold
            (and already returned its reserved share), the condition fails, the
            whole transaction cancels, and the caller falls back to recording spend
            WITHOUT decrementing reserved again.
          * `status IN (ACTIVE, RETAINED) OR attribute_not_exists(status)` —
            existence alone is not enough under the PENDING protocol, because its
            endings do not delete the row: a fenced hold becomes
            `EXPIRED_UNCREDITED` and a retired one `RECLAIMED`, both still present.
            `pool_credit_back` has already returned the reservation by then, so a
            settle passing on existence alone returned it a SECOND time and enlarged
            the tenant's effective budget. A transactional (pre-PENDING) hold carries
            no status attribute at all, so the last clause keeps this inert for
            today's data.

            `RETAINED` is admitted for the same reason `ACTIVE` is, and the reason is
            what the two states have in common: the debit committed and the
            reservation has NOT been given back. A retention is ended by an operator
            resolving it, and that resolution IS this settle (or the paired release),
            so the status has to be part of the money transaction rather than flipped
            back to `ACTIVE` first — a crash between two writes left an expired
            `ACTIVE` hold, and the reaper then reclaimed automatically the very
            reservation the retention existed to hold.

            The reaper's `reclaim_hold_txn_item` deliberately does NOT admit
            `RETAINED`, and the asymmetry is the point: a resolution may end a
            retention and the reaper may not.
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
            item["Delete"]["ConditionExpression"] = (
                "attribute_exists(sk) AND "
                "(#st = :active_h OR #st = :retained_h OR attribute_not_exists(#st))"
            )
            item["Delete"]["ExpressionAttributeNames"] = {"#st": "status"}
            item["Delete"]["ExpressionAttributeValues"] = {
                ":active_h": {"S": "ACTIVE"}, ":retained_h": {"S": "RETAINED"}}
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
