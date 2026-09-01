"""Write-discipline guard for the billing path.

FIRST-RUN WORKFLOW
------------------
The registries below are FAIL-CLOSED. On a fresh checkout (or after any
refactor that moves/renames a write site) the inventory test will fail and
print a paste-ready block of fingerprints, e.g.:

    UNKNOWN WRITE SITE -- if intentional, add to ALLOWED_SITES:
        "backend/dynamo/tenant_budgets.py::TenantBudgets.reserve::transact_write_items",

Copy the lines you have *actually reviewed* into ALLOWED_SITES. Never
wildcard. The whole point is that a new write to the budgets table cannot
land without a human reading this file.

Fingerprint format:  "<module-relpath>::<enclosing qualname>::<call name>"
"""

import ast
import re
from pathlib import Path

import pytest

from tests import billing_guards
from tests.billing_guards import analyze_module

REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------- registries

SCANNED_FILES = {
    "backend/mvp/_pipeline.py",
    "backend/dynamo/tenant_budgets.py",
    "backend/dynamo/user_tenants.py",
    "backend/migrations/backfill_pool_headroom.py",
    "backend/migrations/pool_ceiling_migration.py",
}

# Reviewed write sites. Seeded from the current design:
#   - reserve():  transact_write_items, fresh uuid token, CAS on
#                 pool_reserved_microusd + attribute_not_exists(reservation_id)
#   - settle():   transact_write_items, caller-stable `token`,
#                 attribute_exists(reservation_id)
#   - settle_settled_only(): transact_write_items, token f"{token}-so"
#   - _seed_pool_row(): create-only put_item (attribute_not_exists(tenant_id))
# Fingerprints are TUPLES (module, enclosing-qualname, api) — the engine's
# WriteSite.fingerprint. Seeded from the REAL code after reviewing each site
# against A2/A5 (see the review note beside each).
ALLOWED_SITES = {
    "backend/mvp/_pipeline.py": {
        # The pool money mutations. All transactional, all tokened.
        ("backend/mvp/_pipeline.py", "reserve_credit", "transact_write_items"),          # CAS reserve
        # External authorize (Fable authcap). Pool-only CAS reserve: same shape as
        # reserve_credit's pooled path — [pool CAS, HOLD put, RESERVE ledger event
        # (source=external), IDEMP Put] — minus the per-user token debit (an
        # external action is not token-metered). Fresh token per attempt; a
        # cancelled txn writes nothing. The ONLY new money item is the IDEMP Put
        # (attribute_not_exists → "IDEMP row ⟺ hold committed"), giving idempotent
        # authorize. A2: only pool_reserved advances (+amount), gated by the same
        # CAS on (reserved,settled); A5: fresh token, and the reserve is atomic so
        # a lost ack cannot double-reserve (the retry re-reads and re-CASes).
        # Reviewed OK.
        ("backend/mvp/_pipeline.py", "reserve_external_authorization", "transact_write_items"),
        # Pool-less per-model quota reserve (P0-11 / Fable F-3). Same CAS-reserve
        # shape as reserve_credit: [user_txn, *quota_lines], fresh token per
        # attempt, cancelled transaction writes nothing. No pool counter touched
        # (quota counters only) — A2/A5 reviewed OK.
        ("backend/mvp/_pipeline.py", "_reserve_quota_without_pool", "transact_write_items"),
        ("backend/mvp/_pipeline.py", "_settle_pool_side", "transact_write_items"),        # settle (stable token)
        ("backend/mvp/_pipeline.py", "ReservationContext.release_pool", "transact_write_items"),  # release
        # The same release, re-attempted after a cancellation whose reason was
        # contention rather than a failed condition. Reviewed against A2/A5: it
        # submits the IDENTICAL item list the release built — no counter is computed
        # a second time — and the hold Delete's `attribute_exists(sk)` plus the
        # terminal's `attribute_not_exists` remain the latch, so a retry that races
        # a reaper RECLAIM or a settle cancels and writes nothing. Fresh token per
        # attempt, which is correct here for the same reason it is on the release
        # itself: the token dedupes botocore's transparent retry of ONE attempt, and
        # the conditions — not the token — are what make the whole sequence
        # at-most-once.
        ("backend/mvp/_pipeline.py", "ReservationContext._retry_release", "transact_write_items"),
        ("backend/mvp/_pipeline.py", "_sweep_one_period", "transact_write_items"),        # reaper reclaim
        # Ledger P2: recovers spend after the reaper reclaimed the hold first
        # (RECLAIM terminal). Writes [settled-only counter (+actual, reserved
        # untouched — reaper already returned it), LATE_SETTLE ledger Put (distinct
        # sk, attribute_not_exists), ConditionCheck terminal-is-RECLAIM]. A2: the
        # counter only advances settled by `actual`, never re-touches reserved, so
        # no double-return. A5: STABLE token (_derived_token(token,"late-settle"))
        # so a lost-ack retry of the recovery dedupes to the same write. Reviewed OK.
        ("backend/mvp/_pipeline.py", "_recover_spend_via_late_settle", "transact_write_items"),
        # PENDING-protocol commit (docs/design/pending-protocol.md, PR-1): the
        # 2-item TransactWriteItems [pool conditional debit, SEPARATE marker Put
        # (attribute_not_exists)]. Only pool_reserved advances (+amount), gated by
        # the headroom condition (A2/I1'); idempotency is the marker Put's
        # attribute_not_exists, NOT a token (EXPECTED_TOKEN_KIND "none"). Inert until
        # STRATOCLAVE_RESERVE_PROTOCOL=pending. Reviewed OK.
        ("backend/mvp/_pipeline.py", "_pending_commit_transact", "transact_write_items"),
        # A non-counter delete: removes an amount<=0 HOLD row only; does NOT
        # touch the BUDGET row / counters (reviewed — see _sweep_one_period).
        ("backend/mvp/_pipeline.py", "_sweep_one_period", "delete_item"),
        # Retained-hold release (C8.3's own resolution path): gives back a
        # retention's per-user token debit + per-model quota reservation once
        # the pool side is already confirmed released. Writes ONLY
        # UserTenants.credit_used (via reverse_reservation_txn_item's own
        # underflow-guarded item) and the per-model quota `used` rows
        # (build_reverse_txn_items) — never a pool counter, and it runs after
        # `_resolution_outcome` has already raised on anything but a
        # confirmed RELEASE, so there is nothing left for this write to race.
        # Best-effort: a failure here is logged, not raised, since undoing the
        # already-landed pool release would be worse than leaving a counter to
        # reconcile by hand. Reviewed OK.
        ("backend/mvp/_pipeline.py", "_reverse_retained_hold_counters_best_effort", "transact_write_items"),
    },
    "backend/dynamo/tenant_budgets.py": {
        # Seat-scaled ceiling delta (C14.4). A single-item conditional UpdateItem:
        # `ADD pool_limit_microusd :d, pool_headroom_microusd :d` guarded on
        # `sizing = "per_seat"`. It touches NEITHER pool_reserved NOR pool_settled.
        # A2: it cannot perturb the reserve/settle serialisation the proof is about,
        # because it writes none of the counters the proof reasons over; and being a
        # pure ADD rather than a CAS on a read-back snapshot, it composes with a
        # concurrent reserve's own headroom ADD instead of clobbering it — the exact
        # failure the headroom design exists to avoid.
        # A5: no dedup token, and none is correct here. This write is deliberately
        # NOT idempotent: a second hire is a second seat, so a replay is a different
        # fact rather than a retry. At-most-once comes from the caller, which invokes
        # it once per COMMITTED membership transition. A lost ack therefore leaves
        # the ceiling one seat SMALLER than the seat count — under-granting capacity,
        # which is the safe direction (a refusal, never an over-admission).
        # Reviewed OK.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.adjust_pool_for_seat_delta", "update_item"),
        # _seed_pool_row: the ONE place a pool row comes into existence, and a
        # CREATE-ONLY put_item — ConditionExpression attribute_not_exists(tenant_id),
        # so it either writes a row that did not exist or writes nothing at all.
        # The reserved=settled=0 literals are therefore not a rewrite of anybody's
        # counters: there is no prior row for them to overwrite. A2 is about
        # mutations of a live counter, and this write cannot reach one.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository._seed_pool_row", "put_item"),
        # set_manual_limit: the operator's figure. SET manual_limit ADD pool_limit
        # :delta, pool_headroom :delta, where delta is the BASELINE delta, under a
        # CAS on the three values that delta was computed from (seat_count, the
        # prior manual_limit including its absence, and pool_limit). Names neither
        # protected counter in any of its expressions, so A2 is not engaged; it
        # reads them not at all. Race-safe for the same reason the seat delta is:
        # the money moves as an ADD, so a concurrent reserve's own headroom ADD
        # composes with it instead of being clobbered.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.set_manual_limit", "update_item"),
        # clear_manual_limit: the reversal, REMOVE manual_limit with the same ADD
        # of the baseline delta under the same CAS. Same review as above, and the
        # same reason A2 is not engaged. It is a separate method rather than a
        # sentinel value because zero is a legal figure meaning "refuse every
        # request", so there is no in-band value left to mean "follow the seats".
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.clear_manual_limit", "update_item"),
        # record_rate_in_force: put_item of the singleton row recording the seat
        # rate the stored ceilings were computed at. Not a pool row and not a pool
        # counter — it is the fact the boot-time check compares the configured rate
        # against, so that changing the rate cannot silently restate every
        # seat-tracked ceiling. Money-neutral.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.record_rate_in_force", "put_item"),
        # reconcile_headroom (Fable review finding 2): value-repairs pool_headroom
        # to `limit - reserved - settled` under a CAS (attribute_not_exists OR
        # pool_headroom = :observed). Reads reserved/settled in Python only; its
        # UpdateExpression SETs pool_headroom (+ updated_at) and NEVER names the
        # protected counters — check_non_mutating_counter_update enforces that.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.reconcile_headroom", "update_item"),
        # PENDING protocol (docs/design/pending-protocol.md, PR-1) — the marker is
        # now a SEPARATE fixed-size item (SK=MARKER#<hold_id>), written ATOMICALLY
        # with the pool debit in a 2-item TransactWriteItems (the rejected
        # marker-in-the-pool-item map bloated the hot item). The counter mutations
        # are therefore TRANSACTIONAL again, but their idempotency comes from the
        # MARKER conditions, not a ClientRequestToken (see EXPECTED_TOKEN_KIND "none"
        # below): reserve is guarded by the marker Put's attribute_not_exists;
        # credit-back is guarded by the marker's phase CAS (RESERVED->SETTLED). Both
        # are inert until STRATOCLAVE_RESERVE_PROTOCOL=pending. Reviewed against A2
        # (reserve: headroom-gated ADD; credit-back: exact +amount from the immutable
        # marker, phase CAS makes it exactly-once) — A2/I1' OK.
        #   * pool_credit_back: exactly-once credit-back — [pool ADD +amount, marker
        #     phase CAS RESERVED->SETTLED + TTL]; the phase CAS is the arbiter, a
        #     second credit CCFs on the marker and the whole txn cancels.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.pool_credit_back", "transact_write_items"),
        # marker_settle_best_effort: cleanup-only RESERVED->SETTLED + TTL on the
        # SEPARATE marker item (settle/release/reclaim already returned headroom in
        # their own txn). Touches NO pool counter — only the marker item's phase/ttl
        # — so it is a plain reviewed site, not a counter write. Guarded on
        # marker_phase = RESERVED; money-neutral (see the method docstring).
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.marker_settle_best_effort", "update_item"),
        # PENDING status transitions: single-item conditional SET of `status`
        # only; NEVER name a pool counter (verified structurally — they are NOT in
        # COUNTER_FUNCTIONS). Put of a PENDING hold row (no counter, like
        # hold_put_txn_item's transactional sibling).
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.hold_put_pending", "put_item"),
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository._status_transition", "update_item"),
        # Retaining a hold instead of reclaiming it (C8.3): the SAME shape as the
        # transitions above — a single-item conditional SET of `status` (plus a
        # timestamp), naming no pool counter, so it is NOT in COUNTER_FUNCTIONS and
        # A2 is not engaged. It is written out separately from `_status_transition`
        # only because its condition accepts a row with NO status attribute (the
        # pre-PENDING implicit ACTIVE), which that helper cannot express. What makes
        # it sound is what it does not do: no money moves, and the money it declines
        # to move is already where it belongs. Reviewed OK.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.hold_retain", "update_item"),
        # Recording that a provider call departed and its outcome was never seen
        # (C8.3). Same shape again: a single-item conditional SET of two descriptive
        # attributes, naming no pool counter, so it is NOT in COUNTER_FUNCTIONS and A2
        # is not engaged. It is the fact the reaper's retention branch reads, and it
        # exists because that branch was reading an attribute nothing wrote — see
        # `test_money_branches_on_written_facts.py`, which is what now refuses that
        # shape of defect rather than a reviewer having to notice it. Reviewed OK.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.hold_mark_departed", "update_item"),
    },
    # backend/migrations/backfill_pool_headroom.py makes NO raw write of its own
    # (see COUNTER_FUNCTIONS note): it backfills by delegating to the reviewed
    # reconcile_headroom update, so it has no write site to allow here.
    "backend/migrations/pool_ceiling_migration.py": {
        # The five phases of the ceiling migration. Every one is a single-item
        # CONDITIONAL UpdateItem over one BUDGET row, and NONE of them names
        # pool_reserved_microusd or pool_settled_microusd anywhere in the module --
        # so none is a counter mutation and A2 is not engaged by any of them. What
        # each one is reviewed for instead is that it cannot restate a figure from
        # a stale read: the migration runs against a live table, so every write is
        # guarded on what it read.
        #   * M1 seeds the stored seat rate, guarded
        #     attribute_not_exists(seat_rate_microusd) -- a concurrent seed is never
        #     doubled, and a row already carrying a DIFFERENT rate is left for the
        #     reconciler to flag rather than overwritten.
        ("backend/migrations/pool_ceiling_migration.py", "phase_m1_add_attributes", "update_item"),
        #   * M2 backfills seat_count and, for a row holding an operator's figure,
        #     manual_limit. Guarded on the values it classified from, so a
        #     membership delta or an operator set that landed under it loses the CAS
        #     and the row waits for the next pass instead of being written from a
        #     stale read. Moves no money: it writes the row's COMPOSITION, and
        #     pool_limit is untouched.
        ("backend/migrations/pool_ceiling_migration.py", "phase_m2_backfill", "update_item"),
        #   * M3 repairs a row M2 could not classify, guarded
        #     attribute_not_exists(manual_limit_microusd) AND pool_limit = :observed
        #     -- fail-stale, so a row a concurrent backfill has since fixed is not
        #     overwritten with a figure derived from the total M3 read. Also moves
        #     no money.
        ("backend/migrations/pool_ceiling_migration.py", "phase_m3_cutover", "update_item"),
        #   * M4 REMOVEs the dead `sizing` attribute. Money-neutral by construction
        #     -- nothing reads it and nothing derives from it -- and gated behind a
        #     clean reconciler pass because it is the point of no return for reading
        #     the old shape.
        ("backend/migrations/pool_ceiling_migration.py", "phase_m4_drop_sizing", "update_item"),
        #   * The rate change is the only one that moves money, and the only place
        #     the rate in force may move. It recomputes a seat-tracked row at the
        #     new rate as `ADD pool_limit :d, pool_headroom :d` -- an ADD, so a live
        #     reserve's own headroom ADD composes with it rather than being
        #     clobbered -- under a CAS on the two figures the delta was computed
        #     from (pool_limit and seat_count). It moves pool_limit and
        #     pool_headroom by the SAME delta, which is the identity
        #     headroom == limit - reserved - settled being preserved without either
        #     protected counter being read or written. A row holding an operator's
        #     figure is skipped outright.
        ("backend/migrations/pool_ceiling_migration.py", "recompute_seat_tracked_rows", "update_item"),
    },
    "backend/dynamo/user_tenants.py": {
        # These write the per-USER token-balance row (user_id/tenant_id), NOT
        # the pool BUDGET counters. Reviewed: none carry pool_*_microusd.
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.ensure", "put_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.ensure", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.reserve", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.refund", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.overwrite_credit", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.switch_tenant", "transact_write_items"),
        # archive_membership: marks a membership row archived. Touches the per-USER
        # membership row, never the pool BUDGET row, and carries no pool_*_microusd
        # — the pool side of a membership change is adjust_pool_for_seat_delta,
        # reviewed above. Reviewed: same class as the ensure/reserve/refund writes.
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.archive_membership", "update_item"),
    },
}

# put_item calls that are *allowed* to touch counter attributes, because
# they read-modify-write the whole row. The engine additionally rejects any
# counter attribute in these Items whose value is a *constant* (a literal 0
# in a preserving put means someone replaced the read-back value).
#
# Empty, and deliberately so: no write rewrites a live pool row wholesale any
# more. The one that used to -- set_pool_limit's create branch -- is now
# `_seed_pool_row`, which is CREATE-ONLY rather than preserving and belongs in
# CREATE_ONLY_PUTS below. Putting it here instead would have passed, and passed
# vacuously: the "reads the row first" check is satisfied by the `.get` on the
# ClientError response rather than by any read of the row, and the "no constant
# counter" check does not see `Decimal(0)` as a constant. Two checks that both
# say nothing about the site they are guarding.
PRESERVING_PUTS: dict = {}

# put_item calls allowed to carry counter literals because their own
# ConditionExpression forbids the row already existing, so they can only create.
# The engine verifies the condition rather than taking the entry's word for it.
CREATE_ONLY_PUTS = {
    "backend/dynamo/tenant_budgets.py": {
        # The ONE place a pool row comes into existence, under
        # attribute_not_exists(tenant_id). reserved=settled=0 are the correct
        # values for a row being created, and cannot overwrite anyone's live
        # counters because the condition refuses an existing row outright. A2
        # governs mutations of a live counter and this write cannot reach one.
        "TenantBudgetsRepository._seed_pool_row",
    },
}

# update_item calls in counter-referencing functions that are allowed BECAUSE
# their own DynamoDB expressions never name pool_reserved/pool_settled — they
# read those counters only in Python to compute a non-counter attribute
# (pool_limit / pool_headroom). check_non_mutating_counter_update enforces the
# "no protected counter in the call's strings" invariant structurally.
READONLY_COUNTER_UPDATES = {
    "backend/dynamo/tenant_budgets.py": {
        # set_manual_limit / clear_manual_limit are NOT here and do not need to
        # be: neither names a protected counter in any of its expressions, nor
        # reads one, so the rule this exception softens never fires on them.
        "TenantBudgetsRepository.reconcile_headroom",
    },
}

# Only these qualnames may mention pool counter attribute names at all. In
# tenant_budgets.py the counters appear in the txn-item BUILDERS (pure dict
# builders — they emit the UpdateExpression the pipeline composes into a
# transact) plus the preserving put and the read-side summary. In _pipeline.py
# they appear in _pool_settle_items / the reserve/settle flow. Each reviewed
# against A2.
COUNTER_FUNCTIONS = {
    "backend/dynamo/tenant_budgets.py": {
        "TenantBudgetsRepository.reserve_txn_item",
        "TenantBudgetsRepository.settle_txn_item",
        "TenantBudgetsRepository.reclaim_hold_txn_item",
        "TenantBudgetsRepository.hold_put_txn_item",
        "TenantBudgetsRepository._seed_pool_row",
        # reconcile_headroom reads the mirrors to recompute the invariant; its
        # write never names a protected counter (see READONLY_COUNTER_UPDATES).
        "TenantBudgetsRepository.reconcile_headroom",
        "TenantBudgetsRepository.pool_summary",
        # PENDING protocol counter writers (docs/design/pending-protocol.md, PR-1).
        # reserve_commit_txn_items BUILDS the pool-debit + marker-Put transact items;
        # pool_credit_back executes the pool-return + marker-phase-CAS transact. Both
        # transactional; idempotency is the marker conditions, not a token (see the
        # "none" entries in EXPECTED_TOKEN_KIND). Reviewed against A2/I1'.
        "TenantBudgetsRepository.reserve_commit_txn_items",
        "TenantBudgetsRepository.pool_credit_back",
        "<module>",  # module docstring names the counters
    },
    "backend/dynamo/user_tenants.py": set(),
    # The ceiling migration names NO protected counter anywhere -- it migrates the
    # row's composition (seat rate, seat count, the operator's figure) and, in the
    # rate change, its ceiling. Empty is the assertion.
    "backend/migrations/pool_ceiling_migration.py": set(),
    "backend/migrations/backfill_pool_headroom.py": {
        # The migration reads limit/reserved/settled in _classify to report the
        # target headroom, and delegates the write to reconcile_headroom (a
        # reviewed repo method). It makes NO raw counter write of its own.
        "_classify",
        "backfill",
        "<module>",
    },
    "backend/mvp/_pipeline.py": {
        # counter attrs appear in the settle/settled-only item builders + flow
        "_pool_settle_items",
        "_settled_only_txn_item",
        "reserve_credit",
        # External authorize reads pool_reserved/settled for the CAS ceiling
        # check (same as reserve_credit) — no counter is written except via the
        # reused reserve_txn_item builder. Reviewed against A2.
        "reserve_external_authorization",
        "_settle_pool_side",
        "_sweep_expired_holds",
        "_sweep_one_period",
        "ReservationContext.release_pool",
        "ReservationContext._retry_release",
        "ReservationContext",
        # PENDING protocol reconciler (docs/design/pending-protocol.md): reads the
        # pool counter (counter-first) + sums ACTIVE holds to compute drift; the
        # actual counter mutation is delegated to the reviewed
        # TenantBudgetsRepository.reconcile_credit_back. No raw counter write here.
        "reconcile_pool",
        "<module>",
    },
}

# Non-transactional counter writes whose no-oversell safety is proven by the
# PENDING protocol formal model (test_pending_protocol_z3 / _stateful), NOT by
# transactional axiom A2. The engine still requires each to carry a
# ConditionExpression (a bare unconditional counter ADD is never allowed). See
# docs/design/pending-protocol.md and billing_guards.check_pending_counter_write.
# (PR-1) The PENDING counter writes are now TRANSACTIONAL (separate-item marker),
# so they are governed by the transact-token discipline (A5) with a reviewed "none"
# token kind — the marker conditions, not a token, provide idempotency — rather
# than the non-transactional check_pending_counter_write path. Nothing remains
# here.
PENDING_COUNTER_WRITES: dict = {}

# transact_write_items token discipline (A5). Keyed by (module -> qualname).
EXPECTED_TOKEN_KIND = {
    "backend/mvp/_pipeline.py": {
        # All four mint the token from _fresh_idempotency_token() (a fresh
        # uuid4), so the static classifier correctly reads them as "fresh".
        # The distinction A5 cares about — settle REUSES its token across its
        # own explicit retry loop (assigned once to `token`, plus a derived
        # f"{token}-so") so a lost-ack retry dedupes — is a *within-call*
        # property the static check can't see. That within-call stability is
        # covered by the settle-once Z3 proof + the disconnect regression tests;
        # here we assert the token is at least freshly-minted per settle (never
        # a hard-coded constant, which the classifier WOULD flag).
        "reserve_credit": "fresh",
        # External authorize: fresh token per attempt (same as reserve_credit).
        # Idempotency comes from the IDEMP Put's attribute_not_exists, not the
        # token — a cancelled txn writes nothing, so a fresh token per attempt is
        # correct and a lost-ack retry re-reads the pool and re-CASes.
        "reserve_external_authorization": "fresh",
        "_reserve_quota_without_pool": "fresh",
        "ReservationContext.release_pool": "fresh",
        "ReservationContext._retry_release": "fresh",
        "_sweep_one_period": "fresh",
        # settle has TWO transact sites: the main settle (fresh-minted `token`,
        # reused across its retry loop) and the settled-only fallback
        # (_derived_token(token,...) = deterministic/stable so a lost-ack
        # dedupes). Both kinds are allowed here.
        "_settle_pool_side": ("fresh", "stable"),
        # Ledger P2 late-settle recovery: FRESH token. Idempotency is the
        # LATE_SETTLE sk's attribute_not_exists (exactly one per hold), NOT the
        # token — a derived/stable token would need byte-identical payloads across
        # retries, which the ledger Put's per-attempt ts_ms breaks (A5 review).
        "_recover_spend_via_late_settle": "fresh",
        # PENDING-protocol commit (PR-1): the 2-item pool-debit + marker-Put
        # transact carries NO ClientRequestToken. Idempotency is the marker Put's
        # attribute_not_exists (a re-issue of the SAME hold CCFs on the marker →
        # RESERVE_ALREADY, no second debit), NOT the token — a stale token's 10-min
        # dedup window would misfire against our own retry window (Fable PR-1
        # Q4-item-1). A lost-ack retry re-issues the idempotent transact. Reviewed.
        "_pending_commit_transact": "none",
        # Retained-hold counter give-back: a fresh token per call, matching
        # every other best-effort release-side write — this is not on the
        # settle-once dedupe path (no lost-ack retry needs to land on the
        # SAME write twice; the underlying items' own conditions make a second
        # attempt from scratch safe regardless).
        "_reverse_retained_hold_counters_best_effort": "fresh",
    },
    "backend/dynamo/tenant_budgets.py": {
        # PENDING-protocol credit-back (PR-1): the 2-item pool-return + marker
        # phase-CAS transact carries NO token. The phase CAS (RESERVED->SETTLED) is
        # the exactly-once arbiter — a second credit cancels on the marker
        # condition, so a lost-ack retry cannot double-return headroom, WITHOUT a
        # token. Reviewed against I1'.
        "TenantBudgetsRepository.pool_credit_back": "none",
    },
    "backend/dynamo/user_tenants.py": {
        # tenant reassignment: idempotent SET (attribute_exists-guarded), not a
        # money ADD; a lost-ack retry is harmless, so no ClientRequestToken is
        # required. Outside the billing settle path / proof scope. Reviewed.
        "UserTenantsRepository.switch_tenant": "none",
    },
}

# Required literal condition fragments that the proof relies on. Keyed by the
# builder qualname whose emitted Update/Delete carries them.
REQUIRED_CONDITIONS = {
    "backend/dynamo/tenant_budgets.py": {
        # The reserve gate is now a single conditional ADD to pool_headroom
        # (headroom == limit - reserved - settled), not a snapshot-all-equal CAS.
        # The proof (test_billing_formal_z3.py::test_headroom_*) relies on the
        # `pool_headroom_microusd >= amount` condition being present.
        "TenantBudgetsRepository.reserve_txn_item": [
            "pool_headroom_microusd",   # the conditional-ADD budget gate
        ],
    },
}

BUDGET_TABLE_MARKERS = ("tenant_budgets", "TenantBudgets", "TENANT_BUDGETS_TABLE")


def _run(module, source=None, *, allowed=None, preserving=None, counters=None,
         readonly_updates=None, pending_writes=None, create_only=None):
    billing_guards.REQUIRED_CONDITIONS = REQUIRED_CONDITIONS
    billing_guards.EXPECTED_TOKEN_KIND = EXPECTED_TOKEN_KIND
    src = source if source is not None else (REPO_ROOT / module).read_text()
    return analyze_module(
        src, module,
        allowed_sites=ALLOWED_SITES.get(module, set()) if allowed is None else allowed,
        preserving_puts=PRESERVING_PUTS.get(module, set()) if preserving is None else preserving,
        counter_registry=COUNTER_FUNCTIONS.get(module, set()) if counters is None else counters,
        readonly_counter_updates=(
            READONLY_COUNTER_UPDATES.get(module, set())
            if readonly_updates is None else readonly_updates),
        pending_counter_writes=(
            PENDING_COUNTER_WRITES.get(module, set())
            if pending_writes is None else pending_writes),
        create_only_puts=(
            CREATE_ONLY_PUTS.get(module, set())
            if create_only is None else create_only),
    )


# ---------------------------------------------------------------- the guard

@pytest.mark.parametrize("module", sorted(SCANNED_FILES))
def test_write_discipline(module):
    violations = _run(module)
    if violations:
        inventory = [v for v in violations if "UNKNOWN WRITE SITE" in v]
        msg = "\n".join(violations)
        if inventory:
            msg += ("\n\n--- paste-ready inventory (review each before "
                    "adding to ALLOWED_SITES) ---\n"
                    + "\n".join(f'    "{re.search(r"::.*$", i) and i.split()[-1]}",'
                                for i in inventory))
        pytest.fail(msg)


def test_no_unscanned_module_touches_budgets_table():
    """FAIL-CLOSED: any NON-TEST module under backend/ that references the
    budgets table AND makes a raw DynamoDB write-API call must be in
    SCANNED_FILES. A new module writing to the table can't bypass the guard.

    Modules that only CALL the repository (e.g. admin_tenants ->
    TenantBudgetsRepository.set_manual_limit) are not raw writers: the write
    discipline is enforced at the repo layer, which IS scanned. Test modules
    are exempt. This is the fail-closed net for a NEW raw write path.
    """
    exempt = {
        "backend/tests/test_billing_write_discipline.py",
        "backend/tests/billing_guards.py",
    }
    write_api_re = re.compile(
        r"\.(put_item|update_item|delete_item|transact_write_items|"
        r"batch_write_item|batch_writer|execute_statement|execute_transaction)\b"
    )
    offenders = []
    for path in (REPO_ROOT / "backend").rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in SCANNED_FILES or rel in exempt:
            continue
        # Skip test modules entirely (they reference the table to exercise it,
        # not to define production write paths).
        if "/tests/" in rel or Path(rel).name.startswith("test_"):
            continue
        text = path.read_text()
        if any(m in text for m in BUDGET_TABLE_MARKERS) and write_api_re.search(text):
            offenders.append(rel)
    assert not offenders, (
        "Non-test modules make raw DynamoDB writes AND reference the budgets "
        f"table but are not scanned (add to SCANNED_FILES + registries): {offenders}"
    )


# ------------------------------------------------- planted-violation self-tests
# If the engine ever stops catching these, THIS suite fails -- the guard
# guards itself.

PLANTED_NONTX_COUNTER = '''
class TenantBudgets:
    def sneaky_add(self, tenant_id, amount):
        self.table.update_item(
            Key={"pk": tenant_id},
            UpdateExpression="ADD pool_reserved_microusd :a",
            ExpressionAttributeValues={":a": amount},
        )
'''

PLANTED_NO_TOKEN = '''
def settle(client, key):
    client.transact_write_items(
        TransactItems=[{"Update": {"TableName": "TenantBudgets", "Key": key,
                                   "UpdateExpression": "SET x = :x"}}],
    )
'''

PLANTED_BARE_DELETE = '''
def purge(client, key):
    client.transact_write_items(
        ClientRequestToken="t",
        TransactItems=[{"Delete": {"TableName": "TenantBudgets", "Key": key}}],
    )
'''

PLANTED_GETATTR = '''
def dispatch(table, method, **kw):
    return getattr(table, method)(**kw)
'''

PLANTED_CONST_IN_PRESERVING_PUT = '''
class TenantBudgets:
    def set_ceiling(self, tenant_id, period, limit):
        existing = self.table.get_item(Key={"pk": tenant_id})  # reads the row
        self.table.put_item(Item={
            "pk": tenant_id,
            "pool_limit_microusd": limit,
            "pool_reserved_microusd": 0,   # BUG: clobbers live reservations
            "pool_settled_microusd": existing.get("pool_settled_microusd", 0),
        })
'''


def _assert_flagged(violations, *needles):
    joined = "\n".join(violations)
    for n in needles:
        assert any(n in v for v in violations), (
            f"engine failed to flag {n!r}; got:\n{joined}")


def test_engine_flags_nontransactional_counter_write():
    v = _run("<planted>", PLANTED_NONTX_COUNTER,
             allowed=set(), preserving=set(), counters=set())
    _assert_flagged(v, "pool_reserved_microusd")
    assert any("transact" in x.lower() or "counter" in x.lower() for x in v)


def test_engine_flags_transact_without_token():
    v = _run("<planted>", PLANTED_NO_TOKEN,
             allowed=set(), preserving=set(), counters=set())
    _assert_flagged(v, "ClientRequestToken")


def test_engine_flags_delete_without_attribute_exists():
    v = _run("<planted>", PLANTED_BARE_DELETE,
             allowed=set(), preserving=set(), counters=set())
    _assert_flagged(v, "attribute_exists")


def test_engine_flags_getattr_dispatch():
    v = _run("<planted>", PLANTED_GETATTR,
             allowed=set(), preserving=set(), counters=set())
    assert any("getattr" in x or "dynamic" in x.lower() for x in v), v


def test_engine_flags_constant_counter_in_preserving_put():
    v = _run("<planted>", PLANTED_CONST_IN_PRESERVING_PUT,
             allowed={"<planted>::TenantBudgets.set_ceiling::put_item"},
             preserving={"TenantBudgets.set_ceiling"},
             counters={"TenantBudgets.set_ceiling"})
    _assert_flagged(v, "pool_reserved_microusd")
    assert any("constant" in x.lower() or "literal" in x.lower() for x in v), v


# The create-only-put exception must NOT become a hole either: an allow-listed
# put_item that carries counter literals but has LOST the condition forbidding an
# existing row is an unconditional whole-row rewrite, and must still be rejected.
PLANTED_CREATE_ONLY_PUT_WITHOUT_CONDITION = '''
class TenantBudgets:
    def _seed_pool_row(self, tenant_id, period, limit):
        item = {
            "tenant_id": tenant_id,
            "pool_limit_microusd": limit,
            "pool_reserved_microusd": 0,
            "pool_settled_microusd": 0,
        }
        self._table.put_item(Item=item)
'''


def test_engine_flags_create_only_put_without_the_not_exists_condition():
    v = _run("<planted>", PLANTED_CREATE_ONLY_PUT_WITHOUT_CONDITION,
             allowed={("<planted>", "TenantBudgets._seed_pool_row", "put_item")},
             preserving=set(), counters={"TenantBudgets._seed_pool_row"},
             create_only={"TenantBudgets._seed_pool_row"})
    _assert_flagged(v, "attribute_not_exists")


def test_engine_accepts_a_create_only_put_that_keeps_its_condition():
    """The mirror of the above: the SAME write, with the condition, is clean --
    so the check above is refusing the missing condition rather than the shape."""
    src = PLANTED_CREATE_ONLY_PUT_WITHOUT_CONDITION.replace(
        "put_item(Item=item)",
        'put_item(Item=item, ConditionExpression="attribute_not_exists(tenant_id)")')
    v = _run("<planted>", src,
             allowed={("<planted>", "TenantBudgets._seed_pool_row", "put_item")},
             preserving=set(), counters={"TenantBudgets._seed_pool_row"},
             create_only={"TenantBudgets._seed_pool_row"})
    assert v == [], v


# The readonly-counter-update exception (reconcile_headroom)
# must NOT become a hole: an allow-listed update_item that actually MUTATES a
# protected counter in its own DynamoDB expression must still be rejected.
PLANTED_READONLY_UPDATE_THAT_MUTATES = '''
class TenantBudgets:
    def set_ceiling(self, tenant_id, period, limit):
        row = self.get(tenant_id, period)  # reads reserved/settled
        self.table.update_item(
            Key={"pk": tenant_id},
            UpdateExpression="ADD pool_reserved_microusd :r SET pool_limit_microusd = :l",
            ExpressionAttributeValues={":r": 1, ":l": limit},
        )
'''


def test_engine_flags_readonly_update_that_actually_mutates_counter():
    v = _run("<planted>", PLANTED_READONLY_UPDATE_THAT_MUTATES,
             allowed={("<planted>", "TenantBudgets.set_ceiling", "update_item")},
             preserving=set(),
             counters={"TenantBudgets.set_ceiling"},
             readonly_updates={"TenantBudgets.set_ceiling"})
    # even though it's allow-listed as read-only, naming a protected counter in
    # the UpdateExpression is an A2 regression the guard must catch.
    _assert_flagged(v, "pool_reserved_microusd")
    assert any("read-only" in x.lower() or "regression" in x.lower()
               or "transactional" in x.lower() for x in v), v
