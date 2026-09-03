"""F4 / R39c — the harness gains ONE fixture, and all five scripts use it.

WHAT DEFECT THIS CLOSES

Today `bench/ledger-latency/seed_tenants.py`, `bench_micro.py`,
`bench_pending_spike.py`, and `bench_itemcount_spike.py` all seed a bench
tenant's pool row the same way:

    repo.set_pool_limit(tenant_id=tid, period=period,
                         pool_limit_microusd=<bare literal>, status="active")

with no `seat_count`, `manual_limit`, or `pool_granted` at all. Once the
quota-raise epic lands (`pool_limit_microusd` must equal
`baseline(seat_count, manual_limit) + pool_granted`), this seeds a row whose
limit its own declared source attributes do not explain — the exact defect
R39c names: "every benchmark inheriting its rows is invalid if it seeds a row
whose source attributes do not explain its limit." The write still succeeds
(nothing today refuses it), so every benchmark built on these rows would
silently measure a row that could never occur through the real admin/
team-lead pool-budget write paths once those paths require the identity to
hold.

WHAT R39C ASKS FOR

One fixture (the F4 design note section 4: `bench/ledger-latency/pool_fixture.py`,
`seed_verified_pool(...)`) that writes the SOURCE attributes and asserts, before
returning:

    pool_limit_microusd    == baseline(seat_count, manual_limit) + pool_granted
    pool_headroom_microusd == pool_limit_microusd - pool_reserved_microusd
                               - pool_settled_microusd

and that all five scripts call it instead of handing `set_pool_limit` a bare
`pool_limit_microusd`. `SEAMS (the integration owner's seam-review document)` B5 confirms this design unchanged: the
fixture must satisfy BOTH identities at once, which needs F1's
(`seat_count`/`manual_limit`, and the stored seat rate B2 adds) and F2's
(`pool_granted`) attributes present on the same row simultaneously — "the
composed state none of the four plans reaches" on its own — so this fixture
is where that composition actually happens, exercised for real only once F1
and F2 have both landed. (`bench_marker_shard_spike.py`'s n>1 shard rows are
deliberately EXCLUDED — the F4 design note section 6 explains why: they are
synthetic rows for a sharding design the repository's own docs reject, not a
tenant's real pool row, so the identity does not apply to them.)

WHY THIS FAILS TODAY

`bench/ledger-latency/pool_fixture.py` does not exist. And even once it does,
four of the five scripts' current source (checked here by static grep, so
this half of the test does not need the fixture to exist to fail correctly
today) still call `set_pool_limit(pool_limit_microusd=...)` directly rather
than the fixture.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ROOT / "bench" / "ledger-latency"
FIXTURE_MODULE = BENCH_DIR / "pool_fixture.py"

#: The five scripts R39c names, and whether each is expected to call the fixture
#: for ITS OWN seeding (bench_marker_shard_spike's n=1 branch does seed a real
#: row and should; its n>1 branch writes synthetic shard rows and should not —
#: see the F4 design note section 6, so that script is checked more narrowly below).
DIRECT_SEED_SCRIPTS = [
    "seed_tenants.py",
    "bench_micro.py",
    "bench_pending_spike.py",
    "bench_itemcount_spike.py",
]

#: The call this epic invalidates: a bare `pool_limit_microusd=` kwarg to
#: `set_pool_limit`, with no `seat_count=`/`manual_limit=` alongside it on the
#: same call. A textual check, deliberately — this is meant to catch exactly
#: the pattern named in the four scripts' current source, not to parse Python.
BARE_SET_POOL_LIMIT = re.compile(
    r"set_pool_limit\(\s*[^)]*?pool_limit_microusd\s*=", re.DOTALL)


def test_the_fixture_module_exists():
    assert FIXTURE_MODULE.exists(), (
        f"{FIXTURE_MODULE} does not exist. R39c requires one fixture "
        f"(the F4 design note section 4: seed_verified_pool) that all five scripts "
        f"call instead of seeding pool_limit_microusd directly."
    )


@pytest.mark.parametrize("script_name", DIRECT_SEED_SCRIPTS)
def test_each_direct_seed_script_uses_the_fixture_not_a_bare_limit(script_name):
    """Each of these four scripts must call `seed_verified_pool` and must NOT
    still call `set_pool_limit` with a bare `pool_limit_microusd=` (its own
    seeding call site, not e.g. bench_marker_shard_spike's n=1 fallback which
    is a different script)."""
    path = BENCH_DIR / script_name
    assert path.exists(), f"{path} is one of the five named scripts and is missing"
    text = path.read_text()
    assert "seed_verified_pool" in text, (
        f"{script_name} does not call seed_verified_pool (R39c's fixture) — "
        f"it must seed its bench tenant's pool row through the fixture so the "
        f"row's limit is verified against its own source attributes before "
        f"any timing starts."
    )
    assert not BARE_SET_POOL_LIMIT.search(text), (
        f"{script_name} still calls set_pool_limit(pool_limit_microusd=...) "
        f"directly — this is exactly the call R39c's fixture must replace, "
        f"since a bare pool_limit_microusd seeds a row no source attribute "
        f"explains once pool_limit is derived."
    )


def test_bench_marker_shard_spike_n1_branch_uses_the_fixture():
    """The n=1 branch reuses the real budget row (unlike n>1's synthetic shard
    rows, which are deliberately excluded — see the module docstring and
    the F4 design note section 6) and so is held to the same standard as the other
    four scripts for THAT branch only."""
    path = BENCH_DIR / "bench_marker_shard_spike.py"
    text = path.read_text()
    assert "seed_verified_pool" in text, (
        "bench_marker_shard_spike.py's n=1 branch (the real budget row, not "
        "the synthetic shard rows) does not call seed_verified_pool."
    )


def test_the_fixture_enforces_both_identities_before_returning(dynamodb_mock, monkeypatch):
    """Behavioural check of the fixture itself, once it exists: seeding with
    source attributes that do NOT explain the limit must raise loudly, not
    write successfully and let the caller's benchmark run against a bad row.

    This needs `bench/` on sys.path (bench is not a package the backend test
    suite otherwise imports from) and moto for the DynamoDB call — both are
    already dependencies of this repository's bench harness and dev test
    suite respectively. Fixed after re-running against the implementation:
    this test's own first draft called `TenantBudgetsRepository()` and
    `seed_verified_pool(...)` without requesting the `dynamodb_mock` fixture
    at all, so no table existed — harmless only because the case it chose
    (below) never reached a DynamoDB call in the first place.

    That original case — `seat_count=3` alongside `manual_limit_microusd=1`
    — does not exercise the identity check this test is named for. The
    fixture's own docstring is explicit that a pool row is seat-tracked XOR
    carries an operator's figure, never both, and it refuses the ambiguous
    call BEFORE writing anything: `ValueError`, not `AssertionError`, and it
    fires first regardless of which combination of values is inconsistent.
    That is a real, correct guard (verified below), just not the one this
    test's name is about.

    The identity check itself sits AFTER a write, comparing the row read
    back against what its own source attributes say it must be. Because
    F1's real writers (`create_seat_tracked_pool`, `set_manual_limit`)
    maintain `pool_limit_microusd == baseline + coalesce(pool_granted, 0)`
    by construction, there is no legal combination of this fixture's own
    arguments that can violate it — the only way to see the check's teeth
    without weakening it is a writer that does NOT maintain the identity,
    which is exactly the class of bug R39c protects a benchmark from timing
    silently. Simulated below by monkeypatching `set_manual_limit` to leave
    the row's limit at the figure asked for `set_manual_limit` to write
    correctly, then corrupt it afterward — standing in for a latent bug in
    that writer, not a caller error."""
    if not FIXTURE_MODULE.exists():
        pytest.skip(f"{FIXTURE_MODULE} does not exist yet — see the module-"
                    f"existence test above, which is the one that must fail "
                    f"until it is written")
    if str(BENCH_DIR) not in sys.path:
        sys.path.insert(0, str(BENCH_DIR))
    import pool_fixture  # type: ignore  # noqa: E402

    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk, current_period

    repo = TenantBudgetsRepository()
    period = current_period()

    # The input guard: a pool row is seat-tracked XOR carries an operator's
    # figure (`dynamo.tenant_budgets.is_seat_tracked`), so giving both is
    # deciding that ambiguity silently rather than the caller deciding it.
    # This refuses before any write, which is why it raises ValueError, not
    # the AssertionError the post-write identity check raises.
    with pytest.raises(ValueError):
        pool_fixture.seed_verified_pool(
            repo, tenant_id="l39c-bad-tenant", period=period,
            seat_count=3, manual_limit_microusd=1, pool_granted_microusd=0,
        )

    # The identity check: force the ONE writer this call goes through to
    # leave a row its own source attributes do not explain, standing in for
    # a latent bug in `set_manual_limit` rather than a caller error.
    from decimal import Decimal

    real_set_manual_limit = TenantBudgetsRepository.set_manual_limit

    def _writes_then_corrupts_the_limit(self, *, tenant_id, period, manual_limit_microusd, status="active"):
        result = real_set_manual_limit(
            self, tenant_id=tenant_id, period=period,
            manual_limit_microusd=manual_limit_microusd, status=status)
        # No plausible bug in the caller's own arguments produces this; it
        # stands for a writer that wrote the wrong figure.
        self._table.update_item(
            Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
            UpdateExpression="SET pool_limit_microusd = :bad",
            ExpressionAttributeValues={":bad": Decimal(1)},
        )
        return result

    monkeypatch.setattr(
        TenantBudgetsRepository, "set_manual_limit", _writes_then_corrupts_the_limit)

    with pytest.raises(AssertionError):
        pool_fixture.seed_verified_pool(
            repo, tenant_id="l39c-bad-tenant", period=period,
            manual_limit_microusd=500_000_000, pool_granted_microusd=0,
        )
