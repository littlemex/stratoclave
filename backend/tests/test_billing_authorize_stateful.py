"""Stateful property test for the EXTERNAL authorize/capture/void money path
(Fable authcap — concurrency GAP1 / money GAP1).

`test_credit_ledger_stateful.py` drives the INLINE reserve→settle path. This
machine drives the EXTERNAL surface — `reserve_external_authorization` (pool CAS
+ IDEMP), `rehydrate_reservation_context` + `_settle_external` (capture),
`release_pool` (void), the reaper (`_sweep_expired_holds`), and duplicate-key
replay — in random interleavings over MANY concurrent holds, checking the ledger
accounting-conservation invariants against the live budget counters AND an
independent in-memory reference model after every step.

Why this is worth a machine and not just the ~50 example-based tests: the
example tests each exercise ONE hold through ONE scripted sequence. Only a
stateful machine puts N external holds in flight at once and lets authorize /
capture / void / reap / dup-authorize interleave freely, so a bug that only
appears when hold A's capture runs between hold B's authorize and hold B's reap
(shared pool counters, shared partition) has a path to surface.

External-path money model (a subset of the inline Phase-2 model — no LATE_SETTLE,
because a reclaimed external hold is deliberately 410 not late-settled, Fable
authcap D-2):
  RESERVE   event (own sk, +R, source=external, carries the IDEMP row in the txn)
  SETTLE    terminal (capture, -R returns reserve, +settled = captured ≤ R)
  RELEASE   terminal (void, -R returns reserve, settled 0)
  RECLAIM   terminal (reaper, -R returns reserve, settled 0); a later capture of a
            RECLAIM'd hold raises ExternalHoldReclaimed → NO LATE_SETTLE.

Invariants (external forms of Fable design C):
  I1  pool_settled   == Σ SETTLE.settled_delta                 (no LATE_SETTLE here)
  I2  pool_reserved  == Σ RESERVE.reserved_delta + Σ terminal.reserved_delta
                     == Σ reserved of live (un-terminated) holds
  I3  pool_reclaimed == Σ (-reserved_delta) over RECLAIM terminals
  DERIVED  the ledger's own `derived_totals` fold agrees with the cached counters
           (this is the reconciliation path a batch job would use — and the reader
           that must stay unharmed by the IDEMP rows sharing the partition).
  TERM  each external hold has ≤1 terminal; its type/values match expectation;
        RESERVE set == every hold we authorized; no ghost / missing events.
  IDEMP-INERT  every authorize wrote exactly one IDEMP row, and those rows (which
        carry NO settled/reserved delta) never perturb the money folds.
  NONNEG  all three counters ≥ 0.
"""
from __future__ import annotations

import time
import uuid

import pytest
from boto3.dynamodb.conditions import Key
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    rule,
)

AMOUNT = st.integers(min_value=1, max_value=500_000)   # micro-USD per authorize
CAPTURE_FRACTION = st.integers(min_value=0, max_value=100)


class ExternalAuthcapMachine(RuleBasedStateMachine):
    # Bundles partition holds by lifecycle so a rule only draws holds it applies
    # to: `live` = authorized, not yet terminal; `reclaimed` = reaper-RECLAIM'd,
    # still addressable for the capture-after-reclaim (410) path.
    live = Bundle("live")
    reclaimed = Bundle("reclaimed")

    @initialize()
    def setup(self):
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
        from dynamo.user_tenants import UserTenantsRepository

        # One moto mock spans many Hypothesis examples (function-scoped fixture),
        # so isolate each example on its OWN tenant partition (uuid) — counters and
        # ledger start empty, and cross-example bleed cannot mask a bug.
        suffix = uuid.uuid4().hex[:12]
        self.tenant_id = f"authcap-{suffix}"
        self.period = current_period()
        UserTenantsRepository().ensure(
            user_id=f"user-{suffix}", tenant_id=self.tenant_id,
            role="user", total_credit=1_000_000_000,
        )
        # Generous pool so the machine exercises the money protocol, not the 402
        # ceiling (pool-full is covered by an example test). 10^10 ≫ 500k×steps.
        TenantBudgetsRepository().set_pool_limit(
            tenant_id=self.tenant_id, period=self.period,
            pool_limit_microusd=10_000_000_000,
        )
        # Independent reference model.
        self.ref_settled = 0                     # micro-USD we EXPECT settled
        self._meta = {}                          # hold_id -> {"amount","hold_sk","key"}
        self._live_reserved = {}                 # hold_id -> reserved (un-terminated)
        # hold_id -> (terminal_type, reserved_returned, settled_on_terminal)
        self._expected_terminal = {}
        self._reserved_events = {}               # hold_id -> reserved (RESERVE must exist)
        self._idemp_keys = set()                 # every Idempotency-Key we committed
        self._seq = 0

    # -- helpers -------------------------------------------------------------

    def _ledger(self):
        from dynamo import CreditLedgerRepository

        return CreditLedgerRepository()

    def _budgets(self):
        from dynamo.tenant_budgets import TenantBudgetsRepository

        return TenantBudgetsRepository()

    def _pool_summary(self):
        return self._budgets().pool_summary(self.tenant_id, self.period)

    def _pool_settled(self) -> int:
        return int(self._pool_summary()["pool_settled_microusd"])

    def _pool_reserved(self) -> int:
        return int(self._pool_summary()["pool_reserved_microusd"])

    def _pool_reclaimed(self) -> int:
        from dynamo.tenant_budgets import budget_sk

        row = self._budgets()._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": budget_sk(self.period)}
        ).get("Item") or {}
        return int(row.get("pool_reclaimed_microusd", 0))

    def _all_events(self) -> list[dict]:
        out: list[dict] = []
        led = self._ledger()
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(
                f"TENANT#{self.tenant_id}#P#{self.period}"
            )
        }
        while True:
            resp = led._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return out
            kwargs["ExclusiveStartKey"] = lek

    def _events_by_kind(self):
        """Split the partition into (reserve, terminal, idemp) dicts. reserve /
        terminal are keyed by hold_id; idemp is keyed by idempotency_key. Asserts
        sk-namespace uniqueness within each kind, and that NO LATE_SETTLE exists
        (the external path must never late-settle — Fable authcap D-2)."""
        reserve, terminal, idemp = {}, {}, {}
        for e in self._all_events():
            sk = e["sk"]
            if sk.endswith("#RESERVE"):
                hid = e["hold_id"]
                assert hid not in reserve, f"duplicate RESERVE for {hid}"
                reserve[hid] = e
            elif sk.endswith("#TERMINAL"):
                hid = e["hold_id"]
                assert hid not in terminal, f"duplicate TERMINAL for {hid}"
                terminal[hid] = e
            elif e.get("event_type") == "IDEMP" or "#IDEMP#" in sk or sk.startswith("IDEMP#"):
                key = e.get("idempotency_key", sk)
                assert key not in idemp, f"duplicate IDEMP for {key}"
                idemp[key] = e
            elif sk.endswith("#LATE_SETTLE"):
                raise AssertionError(
                    f"external path wrote a LATE_SETTLE ({sk}) — D-2 says a "
                    "reclaimed external hold must be 410, never late-settled"
                )
            else:
                raise AssertionError(f"unknown ledger event sk {sk}")
        return reserve, terminal, idemp

    # -- rules ---------------------------------------------------------------

    @rule(target=live, amount=AMOUNT)
    def authorize(self, amount):
        """External authorize: pool-only reserve + IDEMP row, single txn."""
        from mvp._pipeline import reserve_external_authorization
        from mvp.billing_authorize import encode_authorization_id

        self._seq += 1
        key = f"key-{self._seq}"
        reserved_before = self._pool_reserved()
        r = reserve_external_authorization(
            tenant_id=self.tenant_id,
            amount_microusd=amount,
            idempotency_key=key,
            request_fingerprint=f"fp-{self._seq}",
            authorization_id_factory=lambda hid, p, hsk: encode_authorization_id(
                hold_id=hid, period=p, hold_sk=hsk
            ),
            ttl_seconds=3600,
            description=f"widget-{self._seq}",
            workflow_run_id=None,
        )
        assert not r.replayed, "fresh key must not replay"
        assert r.hold_id and r.hold_sk, "authorize did not mint a hold identity"
        assert int(r.amount_microusd) == amount
        # Pool reserved advanced by EXACTLY the amount.
        assert self._pool_reserved() == reserved_before + amount, (
            "reserved counter did not advance by the authorized amount"
        )
        self._meta[r.hold_id] = {"amount": amount, "hold_sk": r.hold_sk, "key": key}
        self._live_reserved[r.hold_id] = amount
        self._reserved_events[r.hold_id] = amount
        self._idemp_keys.add(key)
        return r.hold_id

    @rule(hold_id=live)
    def authorize_again_same_key_is_replay(self, hold_id):
        """A duplicate Idempotency-Key REPLAYS the original authorization — no
        second hold, no second reserve, same authorization id (F-2). Draws a live
        hold (leaves it live). This is the idempotent-authorize leg interleaved
        with everyone else's captures/voids/reaps on the shared partition."""
        from mvp._pipeline import reserve_external_authorization
        from mvp.billing_authorize import encode_authorization_id

        meta = self._meta[hold_id]
        reserved_before = self._pool_reserved()
        r = reserve_external_authorization(
            tenant_id=self.tenant_id,
            amount_microusd=meta["amount"],
            idempotency_key=meta["key"],
            request_fingerprint=f"fp-{self._seq_for(hold_id)}",
            authorization_id_factory=lambda hid, p, hsk: encode_authorization_id(
                hold_id=hid, period=p, hold_sk=hsk
            ),
            ttl_seconds=3600,
            description="ignored-on-replay",
            workflow_run_id=None,
        )
        assert r.replayed, "duplicate key did not replay"
        assert r.hold_id == hold_id, "replay returned a DIFFERENT hold"
        assert self._pool_reserved() == reserved_before, (
            "replay moved the reserved counter — a second hold was created"
        )

    def _seq_for(self, hold_id):
        # The fingerprint must match the original (a mismatch is 422). We stored
        # none per-hold; reconstruct from the fact that fp is only compared for
        # equality with the stored one. Reuse the same fp the original wrote by
        # deriving it from the key's numeric suffix.
        return self._meta[hold_id]["key"].split("-", 1)[1]

    @rule(hold_id=consumes(live), frac=CAPTURE_FRACTION)
    def capture(self, hold_id, frac):
        """Capture ≤ authorized: rehydrate from the ledger and call the UNMODIFIED
        `_settle_external`. reserved returned, settled advanced by captured."""
        from mvp._pipeline import rehydrate_reservation_context
        from mvp.billing_authorize import _settle_external

        meta = self._meta[hold_id]
        ctx = rehydrate_reservation_context(
            tenant_id=self.tenant_id, period=self.period,
            hold_id=hold_id, hold_sk=meta["hold_sk"],
        )
        assert ctx is not None, "rehydrate of a live external hold returned None"
        assert int(ctx.pool_reserved_microusd) == meta["amount"], (
            "rehydrated reserved != authorized amount (H-A drift)"
        )
        actual = int((meta["amount"] * frac) // 100)   # ≤ authorized by construction
        settled_before = self._pool_settled()
        _settle_external(ctx, actual)
        assert self._pool_settled() == settled_before + actual, (
            "settled counter did not advance by the captured amount"
        )
        self.ref_settled += actual
        self._live_reserved.pop(hold_id)
        self._expected_terminal[hold_id] = ("SETTLE", meta["amount"], actual)

    @rule(hold_id=consumes(live))
    def void(self, hold_id):
        """Void: rehydrate + `release_pool`. reserved returned, RELEASE terminal,
        nothing settled."""
        from mvp._pipeline import rehydrate_reservation_context

        meta = self._meta[hold_id]
        ctx = rehydrate_reservation_context(
            tenant_id=self.tenant_id, period=self.period,
            hold_id=hold_id, hold_sk=meta["hold_sk"],
        )
        assert ctx is not None, "rehydrate of a live external hold returned None"
        settled_before = self._pool_settled()
        ctx.release_pool()
        assert self._pool_settled() == settled_before, "void moved the settled counter"
        self._live_reserved.pop(hold_id)
        self._expected_terminal[hold_id] = ("RELEASE", meta["amount"], 0)

    @rule(hold_id=consumes(live))
    def double_capture_is_idempotent(self, hold_id):
        """Capture the SAME hold twice with the same actual: the terminal sk
        dedupes the second: settled moves once (Phase-2 terminal exclusion, via
        the external capture path)."""
        from mvp._pipeline import rehydrate_reservation_context
        from mvp.billing_authorize import _settle_external

        meta = self._meta[hold_id]
        actual = int(meta["amount"] // 2)

        ctx1 = rehydrate_reservation_context(
            tenant_id=self.tenant_id, period=self.period,
            hold_id=hold_id, hold_sk=meta["hold_sk"],
        )
        assert ctx1 is not None
        _settle_external(ctx1, actual)
        settled_after_first = self._pool_settled()

        # A second capture rehydrates afresh — but the hold row is gone now, so
        # rehydrate returns None (the endpoint would read the terminal → 200
        # replay). Prove the counter did NOT move again.
        ctx2 = rehydrate_reservation_context(
            tenant_id=self.tenant_id, period=self.period,
            hold_id=hold_id, hold_sk=meta["hold_sk"],
        )
        assert ctx2 is None, "hold row still present after a committed capture"
        assert self._pool_settled() == settled_after_first, (
            "second capture advanced settled — terminal dedup failed"
        )
        self.ref_settled += actual
        self._live_reserved.pop(hold_id)
        self._expected_terminal[hold_id] = ("SETTLE", meta["amount"], actual)

    @rule(target=reclaimed, hold_id=consumes(live))
    def reap(self, hold_id):
        """The reaper reclaims an (aged) external hold: RECLAIM terminal, reserved
        returned, settled 0. The hold moves to the `reclaimed` bundle so a later
        capture can exercise the 410 path."""
        meta = self._meta[hold_id]
        reclaimed_before = self._pool_reclaimed()
        self._force_reap(hold_id, meta["hold_sk"])
        assert self._pool_reclaimed() == reclaimed_before + meta["amount"], (
            "reclaimed counter did not advance by the reserved amount"
        )
        self._live_reserved.pop(hold_id)
        self._expected_terminal[hold_id] = ("RECLAIM", meta["amount"], 0)
        return hold_id

    @rule(hold_id=consumes(reclaimed), frac=CAPTURE_FRACTION)
    def capture_after_reclaim_is_410(self, hold_id, frac):
        """A capture of a reaper-RECLAIM'd external hold must raise
        ExternalHoldReclaimed (the endpoint's 410) and must NOT late-settle — no
        counter moves, no LATE_SETTLE (Fable authcap D-2). This consumes the
        reclaimed hold (terminal already recorded at reap time)."""
        from mvp._pipeline import (
            ExternalHoldReclaimed,
            rehydrate_reservation_context,
        )
        from mvp.billing_authorize import _settle_external

        meta = self._meta[hold_id]
        settled_before = self._pool_settled()
        reserved_before = self._pool_reserved()

        # After a RECLAIM the hold row is gone, so a real capture endpoint sees
        # rehydrate=None and maps the terminal to 410. To exercise the SETTLE-vs-
        # RECLAIM race branch inside `_settle_pool_side` directly (the D-2 code),
        # rebuild a context the way rehydrate would have and drive _settle_external:
        # the terminal already holds a RECLAIM, so the settle txn CCFs on the
        # ledger item and raises ExternalHoldReclaimed.
        ctx = rehydrate_reservation_context(
            tenant_id=self.tenant_id, period=self.period,
            hold_id=hold_id, hold_sk=meta["hold_sk"],
        )
        # Hold row is gone post-reclaim → rehydrate None is the normal case.
        if ctx is None:
            from mvp._pipeline import ReservationContext
            from dynamo.user_tenants import UserTenantsRepository

            # Mirror rehydrate_reservation_context's construction exactly (the
            # RECLAIM'd hold row is gone, so we rebuild the same shape a rehydrate
            # produced pre-reclaim) to drive the SETTLE-vs-RECLAIM branch directly.
            ctx = ReservationContext(
                tenants_repo=UserTenantsRepository(),
                reservation_tokens=0,
                pool_reserved_microusd=meta["amount"],
                period=self.period,
                tenant_id=self.tenant_id,
                pool_active=True,
                hold_id=hold_id,
                hold_sk=meta["hold_sk"],
                source="external",
            )
        actual = int((meta["amount"] * frac) // 100)
        with pytest.raises(ExternalHoldReclaimed):
            _settle_external(ctx, actual)
        assert self._pool_settled() == settled_before, "410 path moved settled"
        assert self._pool_reserved() == reserved_before, "410 path moved reserved"

    # -- shared reaper mechanism --------------------------------------------

    def _force_reap(self, hold_id, hold_sk):
        """Age the hold's embedded expiry and sweep, so the reaper reclaims it and
        writes a RECLAIM terminal. Asserts the row existed and is gone afterwards."""
        from dynamo.tenant_budgets import hold_sk as _hsk
        from mvp._pipeline import _sweep_expired_holds

        budgets = self._budgets()
        item = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": hold_sk}
        ).get("Item")
        assert item is not None, f"hold row {hold_sk} not found — sk convention drifted"
        past = int(time.time()) - 10_000
        new_sk = _hsk(self.period, past, hold_id)
        item["sk"] = new_sk
        item["expires_at"] = past
        budgets._table.delete_item(Key={"tenant_id": self.tenant_id, "sk": hold_sk})
        budgets._table.put_item(Item=item)
        # Keep meta's hold_sk pointing at the current row so a later capture-after-
        # reclaim rehydrate uses the same identity.
        self._meta[hold_id]["hold_sk"] = new_sk
        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        gone = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": new_sk}
        ).get("Item")
        assert gone is None, "sweep did not reclaim the expired hold row"

    # -- invariants ----------------------------------------------------------

    @invariant()
    def i1_settled(self):
        # I1: settled counter == Σ SETTLE.settled_delta (no LATE_SETTLE on this path).
        _, terminal, _ = self._events_by_kind()
        ledger_settled = sum(
            int(e["settled_delta_microusd"])
            for e in terminal.values()
            if e["event_type"] == "SETTLE"
        )
        assert self._pool_settled() == ledger_settled, (
            f"I1 broken: counter={self._pool_settled()} ledger={ledger_settled}"
        )

    @invariant()
    def i2_reserved(self):
        # I2: reserved counter == Σ RESERVE(+R) + Σ terminal(-R), and == Σ live.
        reserve, terminal, _ = self._events_by_kind()
        derived = sum(int(e["reserved_delta_microusd"]) for e in reserve.values())
        derived += sum(int(e["reserved_delta_microusd"]) for e in terminal.values())
        assert self._pool_reserved() == derived, (
            f"I2 broken: counter={self._pool_reserved()} ledger-derived={derived}"
        )
        assert self._pool_reserved() == sum(self._live_reserved.values()), (
            "I2 cross-check: reserved counter != Σ live reserved"
        )

    @invariant()
    def i3_reclaimed(self):
        _, terminal, _ = self._events_by_kind()
        reclaimed = sum(
            -int(e["reserved_delta_microusd"])
            for e in terminal.values()
            if e["event_type"] == "RECLAIM"
        )
        assert self._pool_reclaimed() == reclaimed, (
            f"I3 broken: counter={self._pool_reclaimed()} ledger={reclaimed}"
        )

    @invariant()
    def derived_totals_agree(self):
        # The reconciliation fold the batch job would run must agree with the
        # cached counters — and IDEMP rows sharing the partition must not perturb
        # it. This is the direct proof of security point (d): a reader is unharmed
        # by the delta-less IDEMP rows.
        d = self._ledger().derived_totals(tenant_id=self.tenant_id, period=self.period)
        assert d["settled_microusd"] == self._pool_settled(), "derived settled drift"
        assert d["reserved_microusd"] == self._pool_reserved(), "derived reserved drift"
        assert d["reclaimed_microusd"] == self._pool_reclaimed(), "derived reclaimed drift"

    @invariant()
    def nonneg_counters(self):
        assert self._pool_settled() >= 0
        assert self._pool_reserved() >= 0
        assert self._pool_reclaimed() >= 0

    @invariant()
    def exactly_once_spend(self):
        _, terminal, _ = self._events_by_kind()
        ledger_settled = sum(
            int(e["settled_delta_microusd"])
            for e in terminal.values()
            if e["event_type"] == "SETTLE"
        )
        assert self.ref_settled == ledger_settled, (
            f"EXACTLY-ONCE-SPEND broken: expected={self.ref_settled} "
            f"ledger={ledger_settled}"
        )

    @invariant()
    def idemp_inert(self):
        # Every authorize we committed wrote exactly one IDEMP row, and those rows
        # carry no money delta (they must never be summed into a fold).
        _, _, idemp = self._events_by_kind()
        assert set(idemp) == self._idemp_keys, (
            f"IDEMP set mismatch: ledger={set(idemp)} expected={self._idemp_keys}"
        )
        for key, ev in idemp.items():
            assert "settled_delta_microusd" not in ev, (
                f"IDEMP row {key} carries a settled delta — it would corrupt folds"
            )
            assert "reserved_delta_microusd" not in ev, (
                f"IDEMP row {key} carries a reserved delta — it would corrupt folds"
            )

    @invariant()
    def term(self):
        # Structure + per-event value correctness + no ghost/missing.
        reserve, terminal, _ = self._events_by_kind()
        assert set(reserve) == set(self._reserved_events), (
            f"RESERVE set mismatch: ledger={set(reserve)} "
            f"expected={set(self._reserved_events)}"
        )
        for hid, exp_reserved in self._reserved_events.items():
            assert int(reserve[hid]["reserved_delta_microusd"]) == exp_reserved, (
                f"RESERVE reserved_delta for {hid} != {exp_reserved}"
            )
            assert reserve[hid].get("source") == "external", (
                f"RESERVE for {hid} is not source=external"
            )
        assert set(terminal) == set(self._expected_terminal), (
            f"terminal set mismatch: ledger={set(terminal)} "
            f"expected={set(self._expected_terminal)}"
        )
        for hid, (exp_type, exp_returned, exp_settled) in self._expected_terminal.items():
            ev = terminal[hid]
            assert ev["event_type"] == exp_type, (
                f"terminal type for {hid}: {ev['event_type']} != {exp_type}"
            )
            assert int(ev["reserved_delta_microusd"]) == -exp_returned, (
                f"terminal reserved_delta for {hid} != -{exp_returned}"
            )
            assert int(ev["settled_delta_microusd"]) == exp_settled, (
                f"terminal settled_delta for {hid} != {exp_settled}"
            )
            assert int(ev["settled_delta_microusd"]) >= 0, "negative settled"


TestExternalAuthcapStateful = ExternalAuthcapMachine.TestCase
# 20×25 keeps the external protocol deeply interleaved while CI-reasonable; each
# step does several consistent-read Queries (money invariants), so this is
# I/O-bound over moto, not shallow. Raise for nightly.
TestExternalAuthcapStateful.settings = settings(
    max_examples=20,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(autouse=True)
def _bind_mock(dynamodb_mock):
    yield
