"""Executable specification of the PENDING protocol (non-transactional reserve).

Companion to ``billing/ledger.py``. Where that module is the transactional
reference the current code runs, THIS module is the reference for the confirmed
non-transactional hot-path design in ``docs/design/pending-protocol.md`` — the
one the item-count / PENDING spikes settled (a single conditional ``UpdateItem``
beats the transaction's ~1,190 ms c=16 tail because it has no
``TransactionConflict`` failure mode). It exists to be pinned down by Z3
(inductive invariant preservation) and Hypothesis (adversarial interleavings).

It trades transactional atomicity for latency, so the correctness machinery IS
the design. The whole thing derives from one rule, fixed first: **when it is
uncertain whether the pool was debited, never credit it back** (crediting an
un-debited hold is oversell = unrecoverable; not crediting a debited orphan is a
leak = recoverable by the reconciler).

MODEL SHAPE (faithful to the shipped design)
---------------------------------------------
This object IS the environment (the fake DynamoDB): it holds the real state
(``pool_reserved`` counter, per-hold ``status``) and ``_debited[hold_id]``.

GHOST PROMOTED TO A REAL MARKER (Fable marker redesign). In the first model
``_debited`` was a pure GHOST — the sweeper/reconciler were forbidden to read it,
which forced the aggregate defer-until-quiescent reconciler (and its livelock). In
the shipped design ``_debited[hold_id]`` is realized by the OBSERVABLE pool marker
``applied.<hold_id>`` (written ATOMICALLY with the debit in one UpdateItem —
``dynamo.tenant_budgets.pool_reserve_update`` / ``pool_credit_back``). So the
reconciler MAY now read it per-hold and credit back exactly once. The old ghost
rule (sweeper must not credit on fence) still holds — the SWEEPER has no reason to
touch the pool — but the RECONCILER's decisiveness comes from the real marker, not
a quiescence guess. ``commit_debit`` writes the marker; settle/release/reap/
reconcile REMOVE it (mirroring the production ``REMOVE applied.<hold_id>``).

We model the CONTENDED counter (``pool_reserved``) only; the settled-counter /
headroom split is already proved in ``test_billing_formal_z3`` and
``test_billing_stateful`` and is orthogonal to the PENDING-specific bookkeeping.

STATUS LIFECYCLE
----------------
    (none) --put_pending--> PENDING --commit(step2)+activate(step3)--> ACTIVE
    PENDING --fence (timed out, still PENDING)--> EXPIRED_UNCREDITED
    PENDING --client saw definitive fail--> FAILED            (optional, leak-safe)
    ACTIVE  --settle--> SETTLED
    ACTIVE  --release--> RELEASED
    ACTIVE  --reap (expiry, credited)--> EXPIRED
    EXPIRED_UNCREDITED --reconcile (aggregate)--> RECLAIMED

Two distinct expiry terminals on purpose: ``ACTIVE -> EXPIRED`` credits the pool
(the debit is known to have happened); ``PENDING -> EXPIRED_UNCREDITED`` touches
the pool NOT AT ALL (the sweeper cannot know the ghost, so it never credits — the
debited-but-orphaned amount leaks until the reconciler recovers it in aggregate).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Status(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"                      # ACTIVE expiry, credited
    EXPIRED_UNCREDITED = "EXPIRED_UNCREDITED"  # PENDING fence, pool untouched
    RECLAIMED = "RECLAIMED"                  # reconciler recovered the leak
    FAILED = "FAILED"                        # definitive step-2 failure (leak-safe)


# A hold's reservation is "given back" to the pool (reserved -= amount) exactly
# once, in one of these terminals. Used to state I1'.
_CREDITED_BACK = frozenset({Status.SETTLED, Status.RELEASED, Status.EXPIRED,
                            Status.RECLAIMED})
# Terminals that end a hold's life without ever having (successfully) held a
# debit that is still outstanding.
_TERMINAL = frozenset({Status.SETTLED, Status.RELEASED, Status.EXPIRED,
                       Status.EXPIRED_UNCREDITED, Status.RECLAIMED, Status.FAILED})


class OversellError(Exception):
    """Raised by the model guard when an operation WOULD oversell. In the real
    system oversell is silent corruption; here we make it loud so a test that
    reaches it fails."""


@dataclass
class _Hold:
    amount: int
    status: Status
    committed_tick: Optional[int] = None   # when step 2 committed (for I-biz timing)


@dataclass
class PendingLedger:
    """Single-pool reference model of the PENDING protocol.

    ``limit`` is the hard ceiling; ``pool_reserved`` is the contended counter the
    hot-path conditional ``UpdateItem`` mutates. ``headroom`` = limit - reserved
    (the settled counter is out of scope here, see module docstring)."""

    limit: int
    pool_reserved: int = 0
    active: bool = True
    _holds: dict[str, _Hold] = field(default_factory=dict)
    # GHOST: environment-only truth of whether step 2 applied the debit. Protocol
    # actors (sweeper/reconciler/client-after-ambiguity) MUST NOT read this.
    _debited: dict[str, bool] = field(default_factory=dict)
    _tick: int = 0

    # -- helpers (environment / test may read anything) ---------------------

    def headroom(self) -> int:
        return self.limit - self.pool_reserved

    def status(self, hold_id: str) -> Optional[Status]:
        h = self._holds.get(hold_id)
        return h.status if h else None

    def advance_time(self, ticks: int = 1) -> None:
        self._tick += ticks

    # -- I1' oracle (TEST-ONLY: reads the ghost) ----------------------------

    def _debited_outstanding_sum(self) -> int:
        """Σ amount over holds where (debit applied) ∧ (not yet credited back).
        This is the ghost-derived value ``pool_reserved`` must always equal
        (invariant I1'). Reads the ghost, so it is an ORACLE for tests, never a
        protocol decision."""
        total = 0
        for hid, h in self._holds.items():
            if self._debited.get(hid, False) and h.status not in _CREDITED_BACK:
                total += h.amount
        return total

    # ======================================================================
    # Protocol step 1: Put HOLD PENDING (write-ahead intent, uncontended).
    # ======================================================================
    def put_pending(self, hold_id: str, amount: int) -> bool:
        """attribute_not_exists insert of a PENDING hold. Returns False on a
        duplicate hold_id (the idempotency anchor: hold_id is derived from the
        Idempotency-Key, so a duplicate Key collides here). MUST precede the pool
        debit — it is the discoverable record every debit is guaranteed to have."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        if hold_id in self._holds:
            return False
        self._holds[hold_id] = _Hold(amount=amount, status=Status.PENDING)
        self._debited[hold_id] = False
        return True

    # ======================================================================
    # Protocol step 2: the single conditional UpdateItem = COMMIT POINT.
    # This method is the FAKE DB executing the write; it sets the ghost.
    # `outcome` is chosen by the environment/adversary:
    #   "commit"           -> write reaches server, client learns success
    #   "reject"           -> condition false (exhausted), client learns 402
    #   "ambiguous_applied"-> write applied, client sees timeout (ghost True)
    #   "ambiguous_lost"   -> write never applied, client sees timeout (ghost False)
    # The two ambiguous outcomes are INDISTINGUISHABLE to the client.
    # ======================================================================
    def commit_debit(self, hold_id: str, outcome: str = "commit") -> str:
        h = self._holds.get(hold_id)
        if h is None or h.status is not Status.PENDING:
            # A retried/al ready-resolved commit is not modelled as re-applying
            # (I4: step 2 is never re-sent; max_attempts=1).
            return "noop"

        def _condition_holds() -> bool:
            return self.active and self.headroom() >= h.amount

        def _apply() -> None:
            self.pool_reserved += h.amount
            self._debited[hold_id] = True
            h.committed_tick = self._tick

        if outcome == "commit":
            if _condition_holds():
                _apply()
                return "committed"       # client will call activate()
            return "rejected"            # 402; hold stays PENDING -> fenced later
        if outcome == "reject":
            # Force the exhausted branch (only valid if condition indeed false).
            if _condition_holds():
                # Environment asked for reject but budget fits: not a legal
                # injection — treat as commit to stay faithful.
                _apply()
                return "committed"
            return "rejected"
        if outcome == "ambiguous_applied":
            # The write DID apply but the ack was lost. Requires the condition to
            # actually hold (else nothing could have applied).
            if _condition_holds():
                _apply()
            return "ambiguous"           # client must NOT activate/retry-debit
        if outcome == "ambiguous_lost":
            # The write never reached the server; ghost stays False.
            return "ambiguous"
        raise ValueError(f"unknown outcome {outcome!r}")

    # ======================================================================
    # Protocol step 3: activate. Conditional on still-PENDING. Off critical path.
    # Actor: the async activator. Does NOT read the ghost.
    # ======================================================================
    def activate(self, hold_id: str) -> bool:
        h = self._holds.get(hold_id)
        if h is None or h.status is not Status.PENDING:
            return False                 # already fenced/terminal -> caller alerts
        h.status = Status.ACTIVE
        return True

    # ======================================================================
    # settle / release: the client holds hold_id, so by axiom A1 the debit
    # committed. Legal on PENDING (pre-activate) or ACTIVE. Returns reservation.
    # ======================================================================
    def settle(self, hold_id: str) -> None:
        self._return_reservation(hold_id, Status.SETTLED,
                                 legal={Status.PENDING, Status.ACTIVE})

    def release(self, hold_id: str) -> None:
        self._return_reservation(hold_id, Status.RELEASED,
                                 legal={Status.PENDING, Status.ACTIVE})

    def _return_reservation(self, hold_id: str, terminal: Status, *, legal) -> None:
        h = self._holds.get(hold_id)
        if h is None or h.status not in legal:
            return                       # idempotent / not applicable
        # A1: possessing hold_id implies the debit applied. If the ghost says it
        # did not, the model has been driven into an impossible state — assert it.
        if not self._debited.get(hold_id, False):
            raise OversellError(
                f"settle/release of {hold_id} whose debit never applied "
                "(violates capability axiom A1)")
        self.pool_reserved -= h.amount
        self._debited[hold_id] = False   # REMOVE the marker (same write, prod)
        h.status = terminal

    # ======================================================================
    # Reaper: ACTIVE expiry. The debit is KNOWN to have applied (hold reached
    # ACTIVE only after a client-success commit), so credit back. Actor does not
    # read the ghost — it relies on the ACTIVE status as the proof of debit.
    # ======================================================================
    def reap_active_expired(self, hold_id: str) -> bool:
        h = self._holds.get(hold_id)
        if h is None or h.status is not Status.ACTIVE:
            return False
        self.pool_reserved -= h.amount
        self._debited[hold_id] = False   # REMOVE the marker (same write, prod)
        h.status = Status.EXPIRED
        return True

    # ======================================================================
    # Sweeper: PENDING fence. The sweeper CANNOT know whether the debit applied
    # (no hold_id capability, ghost unreadable), so it NEVER touches the pool. It
    # only moves PENDING -> EXPIRED_UNCREDITED. A debited-but-fenced hold leaks
    # (bounded, I2) until the reconciler recovers it.
    # `now_tick`/`timeout` model the design constraint timeout >> step-3 horizon.
    # ======================================================================
    def fence_pending_expired(self, hold_id: str, *, timeout: int = 0) -> bool:
        h = self._holds.get(hold_id)
        if h is None or h.status is not Status.PENDING:
            return False
        h.status = Status.EXPIRED_UNCREDITED
        return True

    def mark_failed(self, hold_id: str) -> bool:
        """Optional leak-safe terminal a client MAY write on a DEFINITIVE step-2
        failure. The proof must not depend on it (a client can crash before
        writing it); it only reduces the sweeper's work. Never credits."""
        h = self._holds.get(hold_id)
        if h is None or h.status is not Status.PENDING:
            return False
        h.status = Status.FAILED
        return True

    # ======================================================================
    # Reconciler (cold path): aggregate leak recovery. Counter-first read order,
    # then the hold set. NEVER an admission authority; never reads the ghost.
    # ======================================================================
    def reconcile(self) -> int:
        """MARKER-DRIVEN per-hold leak recovery (the shipped design — Fable marker
        redesign). The ghost `_debited` is now the REAL, observable pool marker
        `applied.<hold_id>` (production `dynamo.tenant_budgets.pool_credit_back`),
        so recovery is DETERMINISTIC per hold, NOT an aggregate-drift guess (which
        livelocked on hot pools). For each EXPIRED_UNCREDITED hold:
          * marker present (debited) -> credit back EXACTLY ONCE (atomic remove-
            marker + add-headroom; a second pass finds no marker -> no double
            credit), then retire to RECLAIMED.
          * marker absent (never debited) -> retire WITHOUT crediting (crediting
            an un-debited hold would oversell). Leak-safe either way.
        No defer/hysteresis is needed: the marker resolves the debited/undebited
        ambiguity that forced the old aggregate reconciler to wait for quiescence.
        Returns the total recovered."""
        recovered = 0
        for hid, h in list(self._holds.items()):
            if h.status is not Status.EXPIRED_UNCREDITED:
                continue
            if self._debited.get(hid, False):
                # marker present: credit back exactly once (remove marker + add).
                self.pool_reserved -= h.amount
                self._debited[hid] = False        # REMOVE the marker (atomic in prod)
                recovered += h.amount
            h.status = Status.RECLAIMED             # retire (stops rescan)
        return recovered

    # -- invariant checks (TEST oracles) ------------------------------------

    def check_I1(self) -> None:
        """I1' (no oversell): pool_reserved == Σ debited-and-not-credited-back."""
        expected = self._debited_outstanding_sum()
        if self.pool_reserved != expected:
            raise AssertionError(
                f"I1' violated: pool_reserved={self.pool_reserved} "
                f"!= debited-outstanding={expected}")

    def outstanding_leak(self) -> int:
        """I2 witness: debited holds with no live entitlement and not yet
        reclaimed (the recoverable leak). Reads the ghost — test oracle only."""
        leak = 0
        for hid, h in self._holds.items():
            if (self._debited.get(hid, False)
                    and h.status is Status.EXPIRED_UNCREDITED):
                leak += h.amount
        return leak

    def is_quiescent(self) -> bool:
        """I5: no PENDING left and no recoverable leak outstanding."""
        no_pending = all(h.status is not Status.PENDING for h in self._holds.values())
        return no_pending and self.outstanding_leak() == 0
