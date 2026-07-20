"""Differential oracle (stage 1): the transactional GOLDEN reference vs the PENDING
protocol, driven by the SAME logical operation sequence, compared via an
abstraction alpha (Fable golden-reference migration design, docs/design/
pending-protocol.md).

WHAT THIS PROVES. Over sampled interleavings of the money operations — including
PENDING's in-flight intermediate states (write-ahead put, uncommitted commit,
pre-activate, duplicate-commit replay) — the two implementations agree on:
  * alpha_reserved  — the COMMITTED contended `reserved` counter; and
  * the admission VERDICT (admit vs reject) of each reserve,
INCLUDING when prior spend (`settled`) has eaten into the ceiling.
This is the "effect equivalence" leg of the migration. The "intent equivalence"
leg (the production write-set oracle) is separate and, per Fable review 1, review 1
does NOT close on this test alone — it closes on THIS green AND the write-set
oracle running.

FIDELITY SCOPE (Fable review 1). `BillingLedger` (golden) tracks reserved + settled
+ headroom; `PendingLedger` models the contended reserved counter and takes
`limit` as its ceiling. settled is NOT a native pending counter, but settled DOES
feed admission (golden's ceiling is `reserved + settled + amt <= limit`), and
admission changes the reserved trajectory — so settled CANNOT be ignored (an
earlier version of this test wrongly claimed it was orthogonal; Fable rejected
that). We therefore inject settled ADVERSARIALLY: when the golden books spend
(settle actual>0), we lower the pending model's effective `limit` by the same
amount, making pending's ceiling `reserved + amt <= (limit - settled)` ==
golden's `reserved + settled + amt <= limit`. Admission parity is then asserted in
the settled>0 regime too. This closes "settled feeds the ceiling identically"
without giving the pending model a settled counter it does not have.

α is defined on COMMITTED reserved, so in-flight PENDING holds (put but not yet
committed) contribute 0 to α on BOTH sides and the parity invariant still holds
while a hold is mid-flight.
"""
from __future__ import annotations

from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    rule,
)

from billing.ledger import BillingLedger, LimitExceeded
from billing.pending_protocol import PendingLedger

AMOUNTS = st.integers(min_value=1, max_value=1_000)
LIMITS = st.integers(min_value=0, max_value=5_000)


class DifferentialMachine(RuleBasedStateMachine):
    """Drives one op sequence through BOTH implementations. PENDING is driven in
    SEPARATE stages (put / commit / activate) so its in-flight intermediate states
    are part of the sampled space (Fable review 1 hole B)."""

    # All bundles declared up front (a rule may reference any of them). Lifecycle:
    #   staged --commit--> committed --activate--> live --settle/release/reap--> terminal
    #                          \--fence(crash)--> fenced --reconcile--> terminal
    staged = Bundle("staged")        # pending: put but NOT yet committed (in-flight)
    committed = Bundle("committed")  # pending: committed but NOT yet activated
    live = Bundle("live")            # committed + activated in BOTH (golden hold + pend hid)
    fenced = Bundle("fenced")        # committed hold that crashed pre-activate, fenced
    terminal = Bundle("terminal")    # holds past a terminal (settled/released/reaped/reconciled)

    @initialize(limit=LIMITS)
    def init(self, limit):
        self.gold = BillingLedger(limit=limit)
        self.pend = PendingLedger(limit=limit)
        self._n = 0
        self._fenced: dict = {}   # hid -> {gold, amount} for committed-then-fenced holds

    def _fresh(self) -> str:
        self._n += 1
        return f"h{self._n}"

    # -- PENDING stage 1: write-ahead put (no money moves yet) --------------
    @rule(target=staged, amount=AMOUNTS)
    def put_pending(self, amount):
        hid = self._fresh()
        assert self.pend.put_pending(hid, amount)
        return {"hid": hid, "amount": amount}

    # -- PENDING stage 2: commit. This is the money move + admission verdict.
    #    The GOLDEN reserve happens HERE (same admission point), so both decide
    #    admit/reject against their (settled-adjusted) ceiling at the same moment.
    @rule(target=committed, s=consumes(staged))
    def commit(self, s):
        amount = s["amount"]
        gold_admitted = True
        gold_hold = None
        try:
            gold_hold = self.gold.reserve(amount)
        except LimitExceeded:
            gold_admitted = False
        res = self.pend.commit_debit(s["hid"], "commit")
        pend_admitted = (res == "committed")
        # ADMISSION PARITY — must hold in the settled>0 regime too (pend.limit was
        # lowered by settled on every prior actual>0 settle).
        assert gold_admitted == pend_admitted, (
            f"admission diverged: gold={gold_admitted} pend={pend_admitted} "
            f"amount={amount} gold_reserved={self.gold.reserved()} "
            f"gold_settled={self.gold.settled_total()} gold_limit={self.gold.limit()} "
            f"pend_reserved={self.pend.pool_reserved} pend_limit={self.pend.limit}")
        if gold_admitted:
            return {"gold": gold_hold, "hid": s["hid"], "amount": amount}
        # REJECTED on both. The pending hold stays PENDING; a later retry (after
        # headroom recovers) is covered by the dedicated focused test
        # test_reject_then_retry_admits_on_both below (Fable review-1b E). It is not
        # folded into this bundle graph because golden minted NO hold on reject, so
        # the retry is a fresh golden reserve paired with a re-commit of the SAME
        # pending hold — a shape cleaner to assert deterministically than to thread
        # through Hypothesis bundles.
        return multiple()

    # -- duplicate commit (idempotent replay): a re-issued commit of the SAME
    #    hold must NOT double-debit AND must report "committed" (Fable review-1b D:
    #    assert the return value too, not only reserved-unchanged). golden has no
    #    equivalent (its reserve minted a fresh id). Replay is exercised at EVERY
    #    lifetime stage (committed / live / after-terminal) to prove the model's
    #    `_debited` idempotency guard matches the production marker's lifetime. -----
    @rule(c=committed)
    def commit_replay_committed(self, c):
        before = self.pend.pool_reserved
        assert self.pend.commit_debit(c["hid"], "commit") == "committed"
        assert self.pend.pool_reserved == before

    @rule(h=live)   # live = activated; does NOT consume (replay must not remove it)
    def commit_replay_live(self, h):
        before = self.pend.pool_reserved
        # already ACTIVE (not PENDING) -> model returns "noop", no re-debit.
        assert self.pend.commit_debit(h["pend"], "commit") == "noop"
        assert self.pend.pool_reserved == before

    # -- PENDING stage 3: activate (no money move) --------------------------
    @rule(target=live, c=consumes(committed))
    def activate(self, c):
        assert self.pend.activate(c["hid"])
        return {"gold": c["gold"], "pend": c["hid"], "amount": c["amount"]}

    # -- (Fable review-1b C + 1c Q1) crash after commit, before activate — SPLIT
    #    into fence and reconcile as SEPARATE rules, so the "fenced but not yet
    #    reconciled" LEAK WINDOW exists between steps and other ops interleave in
    #    it (proving a fenced marker does not corrupt other holds' headroom). While
    #    fenced, the reserved is STILL held on both sides (golden not yet reaped,
    #    pending debit still outstanding), so α-parity holds mid-window. -----------

    # NOTE: fenced holds are tracked on `self._fenced` (hid -> {gold, amount}), NOT a
    # Hypothesis bundle, because reconcile()/reap_expired() are GLOBAL (drain ALL
    # EXPIRED_UNCREDITED at once). A per-item bundle-consume rule would assert a
    # single amount while the batch operation moved the sum — the "recovered == sum"
    # subtlety Fable flagged. So fence ADDS to the set and reconcile drains the WHOLE
    # set in one batch, asserting the summed delta.
    @rule(c=consumes(committed))
    def fence_committed_unactivated(self, c):
        r_gold = self.gold.reserved()
        r_pend = self.pend.pool_reserved
        # pending: fence PENDING -> EXPIRED_UNCREDITED (pool UNTOUCHED). golden: do
        # NOT expire_lease yet — golden's reap_expired() is a GLOBAL drain, so
        # marking it expired now would let an unrelated live-hold `reap` rule reap
        # this fenced hold too (cross-rule interference). Defer golden's
        # expire_lease+reap to reconcile_fenced_batch, where the whole set is drained
        # together. Both sides KEEP the amount reserved while fenced (α-parity).
        assert self.pend.fence_pending_expired(c["hid"])
        assert self.gold.reserved() == r_gold        # golden still holds it (live)
        assert self.pend.pool_reserved == r_pend      # fence did NOT move the pool
        self._fenced[c["hid"]] = {"gold": c["gold"], "amount": c["amount"]}

    # -- (Fable review-1c Q2) a delayed commit landing AFTER fence, BEFORE reconcile
    #    must NOT re-debit / resurrect (else reconcile double-credits). Picks any
    #    currently-fenced hold; EXPIRED_UNCREDITED status -> commit is a noop. -------
    @rule()
    def commit_replay_fenced(self):
        if not self._fenced:
            return
        hid = next(iter(self._fenced))
        before = self.pend.pool_reserved
        assert self.pend.commit_debit(hid, "commit") == "noop"
        assert self.pend.pool_reserved == before

    # -- reconcile the WHOLE fenced batch at once (matches global reconcile()). ----
    @rule()
    def reconcile_fenced_batch(self):
        if not self._fenced:
            return
        total = sum(v["amount"] for v in self._fenced.values())
        r_gold = self.gold.reserved()
        r_pend = self.pend.pool_reserved
        # expire the fenced golden holds NOW, then drain — so reap_expired only
        # reaps THIS fenced set (no live hold is expired at this point).
        for v in self._fenced.values():
            self.gold.expire_lease(v["gold"])
        self.gold.reap_expired()                     # golden: drains the fenced set
        recovered = self.pend.reconcile()            # pending: credits all fenced markers
        assert recovered == total
        assert self.gold.reserved() - r_gold == -total
        assert self.pend.pool_reserved - r_pend == -total
        self._fenced.clear()

    # -- (Fable review-1c F) crash AFTER put_pending, BEFORE commit: a never-
    #    debited hold. The sweeper fences it; it must NOT return any reserved (it
    #    never debited). golden minted nothing. We assert the POOL IS UNCHANGED by
    #    the fence (a reconcile's phantom-credit guard is exercised deterministically
    #    in test_fence_uncommitted_reconcile_credits_zero, where no other fenced hold
    #    is outstanding — `reconcile()` is GLOBAL so a `== 0` assert here would be
    #    wrong when another debited hold is concurrently fenced). ------------------
    @rule(s=consumes(staged))
    def reap_staged_uncommitted(self, s):
        r = self.pend.pool_reserved
        assert self.pend.fence_pending_expired(s["hid"])
        assert self.pend.pool_reserved == r          # un-debited fence moves nothing

    # -- settle with spend: reserved returns by amount in BOTH; golden ALSO books
    #    `actual` into settled, so we lower pending's effective limit by the same
    #    `actual` to keep the admission ceilings identical (settled injection). ---

    @rule(target=terminal, h=consumes(live), actual=AMOUNTS)
    def settle(self, h, actual):
        r_gold = self.gold.reserved()
        r_pend = self.pend.pool_reserved
        self.gold.settle(h["gold"], actual)          # reserved -= amount, settled += actual
        self.pend.settle(h["pend"])                  # reserved -= amount
        self.pend.limit -= actual                    # ADVERSARIAL settled injection
        assert self.gold.reserved() - r_gold == -h["amount"]
        assert self.pend.pool_reserved - r_pend == -h["amount"]
        return {"hid": h["pend"]}

    # -- release: reserved returns by amount, no spend (ceilings unchanged) --
    @rule(target=terminal, h=consumes(live))
    def release(self, h):
        r_gold = self.gold.reserved()
        r_pend = self.pend.pool_reserved
        self.gold.release(h["gold"])
        self.pend.release(h["pend"])
        assert self.gold.reserved() - r_gold == -h["amount"]
        assert self.pend.pool_reserved - r_pend == -h["amount"]
        return {"hid": h["pend"]}

    # -- reap an expired live hold: reservation returns in BOTH, no spend ----
    @rule(target=terminal, h=consumes(live))
    def reap(self, h):
        r_gold = self.gold.reserved()
        r_pend = self.pend.pool_reserved
        self.gold.expire_lease(h["gold"])
        self.gold.reap_expired()
        assert self.pend.reap_active_expired(h["pend"])
        assert self.gold.reserved() - r_gold == -h["amount"]
        assert self.pend.pool_reserved - r_pend == -h["amount"]
        return {"hid": h["pend"]}

    # -- (Fable review-1b D) after-terminal replay: a very-late duplicate commit
    #    of a hold that already reached a terminal must NOT re-debit. This probes
    #    the `_debited` guard's lifetime vs the production marker's lifetime (a
    #    settle/release/reap cleared _debited; a replay must not resurrect a debit).
    @rule(t=terminal)
    def commit_replay_after_terminal(self, t):
        before = self.pend.pool_reserved
        # terminal status (SETTLED/RELEASED/EXPIRED/RECLAIMED) -> "noop", no re-debit.
        assert self.pend.commit_debit(t["hid"], "commit") == "noop"
        assert self.pend.pool_reserved == before

    # -- THE differential invariant: committed reserved identical after each op.
    @invariant()
    def reserved_counters_agree(self):
        assert self.gold.reserved() == self.pend.pool_reserved, (
            f"alpha_reserved diverged: gold={self.gold.reserved()} "
            f"pend={self.pend.pool_reserved}")
        assert self.gold.reserved() >= 0 and self.pend.pool_reserved >= 0


TestDifferentialOracle = DifferentialMachine.TestCase
TestDifferentialOracle.settings = hyp_settings(max_examples=400, stateful_step_count=50,
                                               deadline=None)


def test_admission_ceiling_is_identical_including_settled():
    """Focused: settled eats the ceiling IDENTICALLY on both sides. Reserve 60,
    settle it with actual=50 (settled=50), then a 60 reserve must be REJECTED by
    BOTH (reserved 0 + settled 50 + 60 > 100)."""
    gold = BillingLedger(limit=100)
    pend = PendingLedger(limit=100)
    # reserve 60 on both.
    gh = gold.reserve(60)
    assert pend.put_pending("h1", 60) and pend.commit_debit("h1", "commit") == "committed"
    assert pend.activate("h1")
    # settle with actual=50: reserved -> 0 on both; golden settled=50; pend limit -> 50.
    gold.settle(gh, 50)
    pend.settle("h1")
    pend.limit -= 50
    assert gold.reserved() == pend.pool_reserved == 0
    # now a 60 reserve: golden ceiling 0+50+60 > 100 -> reject; pend 0+60 > 50 -> reject.
    gold_rej = False
    try:
        gold.reserve(60)
    except LimitExceeded:
        gold_rej = True
    assert pend.put_pending("h2", 60)
    pend_rej = (pend.commit_debit("h2", "commit") == "rejected")
    assert gold_rej is True and pend_rej is True
    # a 40 reserve fits both (0+50+40 <= 100 ; 0+40 <= 50).
    assert gold.reserve(40)
    assert pend.put_pending("h3", 40) and pend.commit_debit("h3", "commit") == "committed"
    assert gold.reserved() == pend.pool_reserved == 40


def test_reject_then_retry_admits_on_both():
    """Fable review-1b E: a reserve rejected for lack of headroom, then retried
    AFTER another hold releases, must ADMIT on both. golden minted no hold on the
    reject (fresh reserve on retry); pending's rejected hold stayed PENDING and its
    later commit succeeds. Both admit, reserved matches."""
    gold = BillingLedger(limit=100)
    pend = PendingLedger(limit=100)
    # fill 100.
    gh_big = gold.reserve(100)
    assert pend.put_pending("big", 100) and pend.commit_debit("big", "commit") == "committed"
    assert pend.activate("big")
    # a 50 reserve is REJECTED on both (no headroom).
    gold_rej = False
    try:
        gold.reserve(50)
    except LimitExceeded:
        gold_rej = True
    assert pend.put_pending("x", 50)
    assert pend.commit_debit("x", "commit") == "rejected"
    assert gold_rej is True
    assert gold.reserved() == pend.pool_reserved == 100
    # retry BEFORE headroom recovers: still rejected on both, reserved unchanged.
    gold_rej2 = False
    try:
        gold.reserve(50)
    except LimitExceeded:
        gold_rej2 = True
    assert gold_rej2 is True
    assert pend.commit_debit("x", "commit") == "rejected"
    assert gold.reserved() == pend.pool_reserved == 100
    # release the big hold -> headroom recovers.
    gold.release(gh_big)
    pend.release("big")
    assert gold.reserved() == pend.pool_reserved == 0
    # RETRY: golden does a fresh reserve; pending re-commits the still-PENDING x.
    gh_x = gold.reserve(50)
    assert gh_x is not None
    assert pend.commit_debit("x", "commit") == "committed"
    assert pend.activate("x")
    assert gold.reserved() == pend.pool_reserved == 50   # both admitted the retry


def test_fence_uncommitted_reconcile_credits_zero():
    """Fable review-1c F (phantom-credit guard), deterministic: a hold PUT but never
    committed (never debited) that gets fenced must be credited ZERO by reconcile —
    crediting an un-debited hold would oversell. Isolated (no other fenced hold), so
    the global reconcile()'s return is exactly this hold's contribution: 0."""
    pend = PendingLedger(limit=100)
    assert pend.put_pending("u", 40)          # write-ahead only; NOT committed
    assert pend.pool_reserved == 0            # no debit yet
    assert pend.fence_pending_expired("u")    # crash before commit -> fenced
    assert pend.pool_reserved == 0
    assert pend.reconcile() == 0              # PHANTOM-CREDIT GUARD: never debited
    assert pend.pool_reserved == 0            # still zero — no oversell
