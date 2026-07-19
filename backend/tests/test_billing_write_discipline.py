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
}

# Reviewed write sites. Seeded from the current design:
#   - reserve():  transact_write_items, fresh uuid token, CAS on
#                 pool_reserved_microusd + attribute_not_exists(reservation_id)
#   - settle():   transact_write_items, caller-stable `token`,
#                 attribute_exists(reservation_id)
#   - settle_settled_only(): transact_write_items, token f"{token}-so"
#   - set_pool_limit(): preserving put_item (reads row, writes back counters)
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
        ("backend/mvp/_pipeline.py", "_sweep_one_period", "transact_write_items"),        # reaper reclaim
        # Ledger P2: recovers spend after the reaper reclaimed the hold first
        # (RECLAIM terminal). Writes [settled-only counter (+actual, reserved
        # untouched — reaper already returned it), LATE_SETTLE ledger Put (distinct
        # sk, attribute_not_exists), ConditionCheck terminal-is-RECLAIM]. A2: the
        # counter only advances settled by `actual`, never re-touches reserved, so
        # no double-return. A5: STABLE token (_derived_token(token,"late-settle"))
        # so a lost-ack retry of the recovery dedupes to the same write. Reviewed OK.
        ("backend/mvp/_pipeline.py", "_recover_spend_via_late_settle", "transact_write_items"),
        # A non-counter delete: removes an amount<=0 HOLD row only; does NOT
        # touch the BUDGET row / counters (reviewed — see _sweep_one_period).
        ("backend/mvp/_pipeline.py", "_sweep_one_period", "delete_item"),
    },
    "backend/dynamo/tenant_budgets.py": {
        # set_pool_limit CREATE branch: conditional put_item seeding a brand-new
        # pool row (attribute_not_exists(tenant_id)). reserved=settled=0 literals
        # are correct at creation (there is no prior row to preserve), verified by
        # check_preserving_put's read + constant checks.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.set_pool_limit", "put_item"),
        # set_pool_limit UPDATE branches (Fable review finding 3): the ceiling-CAS
        # (SET pool_limit ADD pool_headroom :delta, guarded pool_limit=:old) and
        # the legacy-repair (SET pool_headroom = computed, guarded
        # attribute_not_exists(pool_headroom)). Both READ reserved/settled in
        # Python to compute a value but their DynamoDB expressions NEVER name the
        # protected counters — verified structurally by
        # check_non_mutating_counter_update. A2 governs mutations, not reads, so a
        # non-transactional update that only moves pool_limit/pool_headroom is
        # sound. Race-safe: the delta ADD composes with concurrent reserve ADDs.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.set_pool_limit", "update_item"),
        # reconcile_headroom (Fable review finding 2): value-repairs pool_headroom
        # to `limit - reserved - settled` under a CAS (attribute_not_exists OR
        # pool_headroom = :observed). Reads reserved/settled in Python only; its
        # UpdateExpression SETs pool_headroom (+ updated_at) and NEVER names the
        # protected counters — check_non_mutating_counter_update enforces that.
        ("backend/dynamo/tenant_budgets.py", "TenantBudgetsRepository.reconcile_headroom", "update_item"),
    },
    # backend/migrations/backfill_pool_headroom.py makes NO raw write of its own
    # (see COUNTER_FUNCTIONS note): it backfills by delegating to the reviewed
    # set_pool_limit preserving put, so it has no write site to allow here.
    "backend/dynamo/user_tenants.py": {
        # These write the per-USER token-balance row (user_id/tenant_id), NOT
        # the pool BUDGET counters. Reviewed: none carry pool_*_microusd.
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.ensure", "put_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.ensure", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.reserve", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.refund", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.overwrite_credit", "update_item"),
        ("backend/dynamo/user_tenants.py", "UserTenantsRepository.switch_tenant", "transact_write_items"),
    },
}

# put_item calls that are *allowed* to touch counter attributes, because
# they read-modify-write the whole row. The engine additionally rejects any
# counter attribute in these Items whose value is a *constant* (a literal 0
# in a preserving put means someone replaced the read-back value).
PRESERVING_PUTS = {
    "backend/dynamo/tenant_budgets.py": {"TenantBudgetsRepository.set_pool_limit"},
}

# update_item calls in counter-referencing functions that are allowed BECAUSE
# their own DynamoDB expressions never name pool_reserved/pool_settled — they
# read those counters only in Python to compute a non-counter attribute
# (pool_limit / pool_headroom). check_non_mutating_counter_update enforces the
# "no protected counter in the call's strings" invariant structurally.
READONLY_COUNTER_UPDATES = {
    "backend/dynamo/tenant_budgets.py": {
        "TenantBudgetsRepository.set_pool_limit",
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
        "TenantBudgetsRepository.set_pool_limit",
        # reconcile_headroom reads the mirrors to recompute the invariant; its
        # write never names a protected counter (see READONLY_COUNTER_UPDATES).
        "TenantBudgetsRepository.reconcile_headroom",
        "TenantBudgetsRepository.pool_summary",
        "<module>",  # module docstring names the counters
    },
    "backend/dynamo/user_tenants.py": set(),
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
        "ReservationContext",
        "<module>",
    },
}

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
         readonly_updates=None):
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
    TenantBudgetsRepository.set_pool_limit) are not raw writers: the write
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
    def set_pool_limit(self, tenant_id, period, limit):
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
             allowed={"<planted>::TenantBudgets.set_pool_limit::put_item"},
             preserving={"TenantBudgets.set_pool_limit"},
             counters={"TenantBudgets.set_pool_limit"})
    _assert_flagged(v, "pool_reserved_microusd")
    assert any("constant" in x.lower() or "literal" in x.lower() for x in v), v


# The readonly-counter-update exception (set_pool_limit / reconcile_headroom)
# must NOT become a hole: an allow-listed update_item that actually MUTATES a
# protected counter in its own DynamoDB expression must still be rejected.
PLANTED_READONLY_UPDATE_THAT_MUTATES = '''
class TenantBudgets:
    def set_pool_limit(self, tenant_id, period, limit):
        row = self.get(tenant_id, period)  # reads reserved/settled
        self.table.update_item(
            Key={"pk": tenant_id},
            UpdateExpression="ADD pool_reserved_microusd :r SET pool_limit_microusd = :l",
            ExpressionAttributeValues={":r": 1, ":l": limit},
        )
'''


def test_engine_flags_readonly_update_that_actually_mutates_counter():
    v = _run("<planted>", PLANTED_READONLY_UPDATE_THAT_MUTATES,
             allowed={("<planted>", "TenantBudgets.set_pool_limit", "update_item")},
             preserving=set(),
             counters={"TenantBudgets.set_pool_limit"},
             readonly_updates={"TenantBudgets.set_pool_limit"})
    # even though it's allow-listed as read-only, naming a protected counter in
    # the UpdateExpression is an A2 regression the guard must catch.
    _assert_flagged(v, "pool_reserved_microusd")
    assert any("read-only" in x.lower() or "regression" in x.lower()
               or "transactional" in x.lower() for x in v), v
