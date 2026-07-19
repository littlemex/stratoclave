"""Executable specification of Stratoclave's pooled-budget accounting.

This is a self-contained, single-process reference implementation of the exact
money logic that ``mvp/_pipeline.py`` + ``dynamo/tenant_budgets.py`` run against
DynamoDB. It exists to be pinned down by formal (Z3) and stateful (Hypothesis)
tests: those tests reason about the *logic*, and this module is the logic in one
place, faithful to the real semantics:

  * Three counters on a pool row: ``reserved`` (R), ``settled`` (S), plus the
    hard ceiling ``limit`` (L). Also ``reclaimed`` (informational, mirrors the
    real ``pool_reclaimed_microusd`` — never affects admission).
  * **reserve(amount)** admits iff ``R + S + amount <= L`` (the Python-side
    ceiling check). On admission it does ``R += amount`` and records a live
    HOLD of ``amount``. This models the optimistic-CAS commit: in a single
    process the read→check→commit is atomic, which is exactly what the real
    ``ConditionExpression: pool_reserved == snapshot`` guarantees after retries
    settle out. Over the ceiling → ``LimitExceeded`` (fail closed).
  * **settle(hold_id, actual)** with ``actual <= reserved`` enforced by the
    caller in prod (reservation is the max plausible cost): ``R -= reserved``,
    ``S += actual``, HOLD deleted. Idempotent per hold via the state machine.
  * **release(hold_id)**: ``R -= reserved``, HOLD deleted, no spend.
  * **reap_expired()** (crash recovery): for each EXPIRED live hold whose owner
    died between reserve and settle, ``R -= amount`` and ``reclaimed += amount``
    — the reservation is returned to the pool. IMPORTANT: unlike a naive model,
    the real reaper does **not** charge spend for a reaped hold (the crashed
    request's usage is not billed); it only frees the reservation. The
    ``attribute_exists(sk)`` delete condition makes reclaim happen at most once
    per hold, so R is never double-subtracted or driven negative.
  * **set_limit(new)**: an admin may lower L below current ``R + S``; the code
    does not claw back committed usage, so ``R + S`` can transiently exceed the
    new L. This is the ONLY way ``R + S > L`` arises in the real system (the
    reaper does not add spend), and it is bounded by the lowering amount.

Concurrency note: the real system is multi-writer over one DynamoDB row; the CAS
condition serialises committing reserves. This module is single-threaded and
therefore already serialised — the Z3 suite is what proves the *concurrent* CAS
can't over-admit. Here we get an executable model to fuzz the full lifecycle
(reserve/settle/release/crash+reap/limit-change) against its reference invariants.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass


class LedgerError(Exception):
    pass


class LimitExceeded(LedgerError):
    """reserve rejected: would push R + S past L (fail closed)."""


class HoldNotFound(LedgerError):
    """settle/release on a hold that is already gone (settled/released/reaped)."""


@dataclass
class _Hold:
    amount: int
    expired: bool = False  # test hook: owner "crashed", lease is in the past


class BillingLedger:
    def __init__(self, limit: int):
        self._limit = int(limit)
        self._reserved = 0
        self._settled = 0
        self._reclaimed = 0
        # Hot-path headroom counter (docs/design/ledger-hot-path.md): the single
        # attribute the real reserve gate reads/writes. Maintained here by the
        # SAME deltas the real code applies so the executable spec proves the
        # invariant `headroom == limit - reserved - settled` holds after every
        # operation of the full randomized lifecycle. Seeded to `limit` (R=S=0).
        self._headroom = int(limit)
        self._live: dict[str, _Hold] = {}
        self._state: dict[str, str] = {}  # hold_id -> live|settled|released|reaped
        self._ids = itertools.count(1)

    # ----- read side -----
    def reserved(self) -> int:
        return self._reserved

    def settled_total(self) -> int:
        return self._settled

    def headroom(self) -> int:
        return self._headroom

    def limit(self) -> int:
        return self._limit

    def hold_state(self, hold_id: str) -> str:
        return self._state.get(hold_id, "unknown")

    # ----- reserve (optimistic-CAS admission) -----
    def reserve(self, amount: int) -> str:
        if amount < 1:
            raise ValueError("amount must be >= 1")
        # The Python-side ceiling check, evaluated against the current row.
        if self._reserved + self._settled + amount > self._limit:
            raise LimitExceeded(
                f"R({self._reserved}) + S({self._settled}) + {amount} > L({self._limit})"
            )
        hold_id = f"h{next(self._ids)}"
        self._reserved += amount
        self._headroom -= amount           # reserve: headroom -= amount
        self._live[hold_id] = _Hold(amount=amount)
        self._state[hold_id] = "live"
        return hold_id

    # ----- settle (return reservation, record actual spend) -----
    def settle(self, hold_id: str, actual: int) -> None:
        hold = self._live.get(hold_id)
        if hold is None:
            raise HoldNotFound(hold_id)
        if actual < 0:
            raise ValueError("actual must be >= 0")
        # `actual` MAY exceed the reservation: the reserve is a heuristic
        # (max_out + input_est), and cache read/write tokens are settled but not
        # reserved at all, so a real settle can bill more than it held. The
        # SET-based settle (ADD reserved -x, settled +actual) stays
        # conservation-correct regardless; the excess just shows up as bounded
        # ceiling overshoot (R+S can exceed L by the overshoot — same class as
        # an admin lowering the limit; see the Z3 CE test). Do NOT reject it.
        self._reserved -= hold.amount
        self._settled += actual
        self._headroom += hold.amount - actual   # settle: headroom += (reserved - actual)
        del self._live[hold_id]
        self._state[hold_id] = "settled"

    # ----- release (error path: return reservation, no spend) -----
    def release(self, hold_id: str) -> None:
        hold = self._live.get(hold_id)
        if hold is None:
            raise HoldNotFound(hold_id)
        self._reserved -= hold.amount
        self._headroom += hold.amount        # release: headroom += amount (no spend)
        del self._live[hold_id]
        self._state[hold_id] = "released"

    # ----- crash-recovery reaper -----
    def expire_lease(self, hold_id: str) -> None:
        """TEST HOOK: mark a live hold's lease as expired (owner 'crashed')."""
        hold = self._live.get(hold_id)
        if hold is None:
            raise HoldNotFound(hold_id)
        hold.expired = True

    def reap_expired(self) -> dict[str, int]:
        """Reclaim every expired live hold: R -= amount (returned to pool).

        Returns {hold_id: reclaimed_amount}. Does NOT charge spend — the real
        reaper only frees the reservation (see module docstring). The
        attribute_exists-guarded delete means a hold is reclaimed at most once;
        here that is inherent because we pop it from `_live`.
        """
        reaped: dict[str, int] = {}
        for hold_id, hold in list(self._live.items()):
            if hold.expired:
                self._reserved -= hold.amount
                self._reclaimed += hold.amount
                self._headroom += hold.amount    # reap: headroom += amount (no spend)
                del self._live[hold_id]
                self._state[hold_id] = "reaped"
                reaped[hold_id] = hold.amount
        return reaped

    # ----- admin -----
    def set_limit(self, new_limit: int) -> None:
        # Ceiling delta-CAS (Fable review finding 3): headroom shifts by the
        # limit delta only; reserved/settled untouched. Mirrors the real
        # `SET pool_limit ADD pool_headroom (:new - :old)`.
        self._headroom += int(new_limit) - self._limit
        self._limit = int(new_limit)
