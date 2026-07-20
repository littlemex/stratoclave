"""Reserve differential oracle (golden-reference migration, stage 1 — the
production "intent equivalence" leg; companion to the offline
tests/test_billing_differential_oracle.py "effect equivalence" leg).

docs/design/pending-protocol.md: `transaction` is the GOLDEN reference (frozen);
`pending` is the path production actually executes for a canary tenant. Before the
pending reserve issues its `TransactWriteItems`, this oracle compares the WRITE-SET
pending is ABOUT TO SEND (its admission verdict + the pool_reserved delta) against
the write-set the GOLDEN transaction reference would produce for the same input,
computed as a PURE function of the pool's pre-state. Purpose: detect any place the
two paths would move money differently, on real traffic, before `transaction` is
deleted.

SAFETY / COST (Fable-designed):
  * NEVER auto-rolls-back. A mismatch is logged + a metric emitted + alarmed
    (fail-open). Money safety is enforced by pending's own condition expressions;
    the oracle is a DETECTOR, never an arbiter.
  * It runs ONLY on the pending reserve path, which itself only runs for a CANARY
    tenant (mvp._pipeline._reserve_protocol_for). So the oracle is per-tenant by
    construction — enabling it does NOT add a read to transaction-mode tenants.
    On the pending path it costs ONE strongly-consistent pool GetItem (the same
    read the golden transaction path does) plus pure arithmetic; a global
    STRATOCLAVE_RESERVE_ORACLE kill-switch (default ON) can disable it entirely.
  * TWO signals (Fable review 2): `reserve_oracle_match` (verdict+delta agreed) and
    `reserve_oracle_mismatch` (disagreed AND the pool did not move between the
    pre-read and the commit — a GENUINE divergence). A disagreement WITH a moved
    pool is `reserve_oracle_race` (a benign TOCTOU artifact, separate non-alarming
    metric) — because the golden prediction is computed off a pre-read snapshot and
    a concurrent reserve/release near the ceiling can flip the verdict without any
    real inequivalence. The delete gate keys on `match_count >= N AND mismatch == 0`
    (NOT merely mismatch == 0, which would pass on zero samples).
  * Compares the abstract MONEY EFFECT (verdict + reserved delta), NOT raw item
    bytes — the two protocols write structurally different items by design.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)


def oracle_enabled() -> bool:
    """Global kill-switch, default ON. The oracle only actually runs on the pending
    reserve path (canary tenants), so "ON" does not touch transaction-mode tenants;
    this switch exists to disable the extra canary read entirely if ever needed.
    Set STRATOCLAVE_RESERVE_ORACLE=false to turn it off."""
    return os.getenv("STRATOCLAVE_RESERVE_ORACLE", "true").lower() != "false"


# The two admission verdicts the reserve write-set encodes.
VERDICT_ADMIT = "admit"       # the pool debit will be attempted / committed
VERDICT_REJECT = "reject"     # genuine exhaustion — nothing debited (402)


@dataclass(frozen=True)
class ReserveWriteSet:
    """The abstract money effect of one reserve, protocol-independent. `verdict` is
    admit/reject; `reserved_delta` is the signed change to pool_reserved this
    reserve intends (0 on reject, +amount on admit). This is α for the reserve
    operation — the quantity the golden and pending must agree on."""

    verdict: str
    reserved_delta_int: int


def golden_predicted_writeset(*, amount_microusd: int,
                              pool_row: Optional[dict[str, Any]]) -> ReserveWriteSet:
    """What the GOLDEN transactional reserve (mvp._pipeline.reserve_external_
    authorization's transaction branch) WOULD write, as a pure function of the
    pre-read pool row. Mirrors that branch's admission gate EXACTLY:

        pool_row is None                     -> (no pool; treated as reject here —
                                                the real path raises NoPool, which
                                                the caller handles before the oracle)
        status != "active"                   -> reject (tenant_pool_exhausted)
        reserved + settled + amount > limit  -> reject (tenant_pool_exhausted)
        else                                 -> admit, reserved_delta = +amount

    This is the FROZEN golden: it must track the transaction branch's ceiling
    check byte-for-byte. If the transaction branch ever changes, this function (and
    the migration) is invalidated — hence the freeze precondition in the design."""
    amount = int(amount_microusd)
    if pool_row is None:
        return ReserveWriteSet(VERDICT_REJECT, 0)
    if str(pool_row.get("status", "active")) != "active":
        return ReserveWriteSet(VERDICT_REJECT, 0)
    limit = int(pool_row.get("pool_limit_microusd", 0))
    reserved = int(pool_row.get("pool_reserved_microusd", 0))
    settled = int(pool_row.get("pool_settled_microusd", 0))
    if reserved + settled + amount > limit:
        return ReserveWriteSet(VERDICT_REJECT, 0)
    return ReserveWriteSet(VERDICT_ADMIT, amount)


def pending_actual_writeset(*, amount_microusd: int, outcome: str,
                            exhausted_sentinel: str, applied_sentinel: str) -> ReserveWriteSet:
    """What the PENDING commit actually did, as the same abstract effect. Only the
    FRESH-admission outcomes are mapped: APPLIED -> admit (+amount), EXHAUSTED ->
    reject (0). The idempotent-replay outcome (ALREADY) is NOT passed here — the
    caller skips the oracle for a replay (its per-call reserved delta is 0, not
    +amount, and the pre-read pool already reflects the prior debit, so it is not
    apples-to-apples). Passing anything but applied/exhausted is a caller bug."""
    if outcome == exhausted_sentinel:
        return ReserveWriteSet(VERDICT_REJECT, 0)
    if outcome == applied_sentinel:
        return ReserveWriteSet(VERDICT_ADMIT, int(amount_microusd))
    raise ValueError(f"pending_actual_writeset: unexpected outcome {outcome!r} "
                     "(only applied/exhausted are oracle-checked; replay is skipped)")


def _concurrent_move(before: Optional[dict[str, Any]], after: Optional[dict[str, Any]],
                     own_reserved_delta: int) -> bool:
    """Did SOMETHING OTHER THAN this reserve's own debit change the pool between the
    pre-read snapshot and a post-commit re-read? `own_reserved_delta` is what THIS
    reserve itself moved (so we subtract it before comparing reserved) — otherwise
    the re-read would always look "moved" by our own commit and mask real mismatches
    as races. A change in settled/limit/status, or in reserved beyond our own delta,
    means a CONCURRENT reserve/release/settle raced us → benign TOCTOU."""
    if before is None or after is None:
        return before is not after   # one side missing (pool created/deleted) = moved
    exp_reserved = int(before.get("pool_reserved_microusd", 0)) + int(own_reserved_delta)
    return (int(after.get("pool_reserved_microusd", 0)) != exp_reserved
            or int(after.get("pool_settled_microusd", 0)) != int(before.get("pool_settled_microusd", 0))
            or int(after.get("pool_limit_microusd", 0)) != int(before.get("pool_limit_microusd", 0))
            or str(after.get("status", "active")) != str(before.get("status", "active")))


def compare_and_log(*, tenant_id: str, period: str, hold_id: str,
                    golden: ReserveWriteSet, pending: ReserveWriteSet,
                    pool_before: Optional[dict[str, Any]] = None,
                    pool_after: Optional[dict[str, Any]] = None) -> str:
    """Compare golden-predicted vs pending-actual write-sets. Returns "match",
    "race", or "mismatch". NEVER raises, NEVER changes control flow — money is
    already decided by pending; the oracle only records.

    On disagreement, `pool_after` (a post-commit re-read taken by the caller)
    disambiguates: if the pool moved by MORE than this reserve's own committed
    delta (i.e. a concurrent op raced), the golden prediction was computed off a
    now-stale snapshot → `reserve_oracle_race` (benign TOCTOU, non-alarming). Only a
    disagreement with NO concurrent move is a genuine `reserve_oracle_mismatch`
    (alarmed, blocks the delete gate)."""
    if golden.verdict == pending.verdict and golden.reserved_delta_int == pending.reserved_delta_int:
        logger.info("reserve_oracle_match", tenant_id=tenant_id, period=period,
                    hold_id=hold_id, verdict=pending.verdict,
                    reserved_delta=pending.reserved_delta_int)
        return "match"
    if _concurrent_move(pool_before, pool_after, pending.reserved_delta_int):
        logger.warning("reserve_oracle_race", tenant_id=tenant_id, period=period,
                       hold_id=hold_id, golden_verdict=golden.verdict,
                       pending_verdict=pending.verdict)
        return "race"
    logger.error("reserve_oracle_mismatch", tenant_id=tenant_id, period=period,
                 hold_id=hold_id,
                 golden_verdict=golden.verdict, pending_verdict=pending.verdict,
                 golden_reserved_delta=golden.reserved_delta_int,
                 pending_reserved_delta=pending.reserved_delta_int)
    return "mismatch"
