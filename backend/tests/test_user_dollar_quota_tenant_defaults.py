"""HANDOFF-PR3 I3 -- the tenant row: `user_dollar_defaults`, `sealed_user_dollar_base`,
`user_dollar_defaults_version`, and the version-clause seal that serializes them.

NAMING, FLAGGED UP FRONT (report this to the integrator): I3 explicitly names
exactly one resolution rule -- the greatest effective period at or below the
query -- and gives the EXACT
DynamoDB shapes for the seal write and the setter's write, but never names the
module, the sealing entry point, or the setter method. Two readings were
possible for WHERE this lives:

  (a) a new standalone module (mirroring `mvp/routing/user_dollar_quota.py`,
      I1's home for the wall's builders), or
  (b) an extension of the EXISTING `dynamo.tenants` module, which already owns
      the `stratoclave-tenants` table and already has a per-user MONEY-adjacent
      default on it (`default_credit`, cited at `dynamo/tenants.py:10` -- I3's
      own reason for the `user_dollar_defaults` field name, "money in the
      name... the same row already carries `default_credit`").

This file commits to (b), the reading the citation supports, and to these
names, chosen by the closest in-repo precedent for each shape:

  - `dynamo.tenants.resolve_user_dollar_default(item, period)` -- PURE, per contract
    amendment A2: resolution takes the already-read row, so a caller that has read once
    cannot be made to read twice. The reading entry point is the seal below.
  - `dynamo.tenants.seal_user_dollar_base(tenant_id, period, *, repo=None)` --
    the read+resolve+conditional-write+retry sequence I3 describes in prose
    with no name of its own. Modelled on `dynamo.tenants.resolve_bound_mode`
    (a module-level function, optional `repo=` for injection) since that is
    the one existing module-level (non-repository-method) function in this
    file. Returns the now-sealed (or already-sealed) base, or `None` if the
    tenant has no configured default for this period (I3: "nothing is sealed
    and no item is emitted").
  - `dynamo.tenants.TenantsRepository.set_user_dollar_default(*, tenant_id,
    effective_period, microusd)` -- the setter, modelled on the sibling
    `TenantBudgetsRepository.set_manual_limit`'s keyword-only, tenant_id=/
    period=-style signature.

If the shipped names differ, the SETUP in every test below (raw `put_item`/
`update_item` against the exact I3 schema, which needs no guessed name) stays
valid; only the single call line naming the guessed function needs a rename.
The exact exception type either refusal path raises is UNSPECIFIED by I3, so
every refusal assertion here catches the broad `Exception` rather than a
guessed class name -- see the report for this flagged as an untested exact
type.
"""
from __future__ import annotations

from decimal import Decimal

import boto3
import pytest

pytest.importorskip("moto")


TENANT_TABLE = "stratoclave-tenants"


@pytest.fixture
def tenants_table(dynamodb_mock):
    return boto3.resource("dynamodb", region_name="us-east-1").Table(TENANT_TABLE)


def _seed_tenant_row(tenants_table, tenant_id: str, **extra):
    item = {"tenant_id": tenant_id, "name": tenant_id, "status": "active", **extra}
    tenants_table.put_item(Item=item)


def _read_row(tenants_table, tenant_id: str) -> dict:
    return tenants_table.get_item(Key={"tenant_id": tenant_id}).get("Item", {})


def _intercept_one_update_item(monkeypatch, repo, *, matches, on_match):
    """Deterministically pin an interleaving without threads, exactly as
    `tests/test_seat_rate_migration_cas_races.py`'s helper of the same name
    does: patch the ONE shared low-level boto3 client so the FIRST
    `update_item` call satisfying `matches(kwargs)` triggers `on_match()`
    (a REAL, synchronous write) before the real call it intercepted proceeds
    -- simulating a concurrent writer landing in the window between the
    intercepted caller's own prior read and this write of its own.
    """
    client = repo._table.meta.client
    original = client.update_item
    fired = {"done": False}

    def wrapped(**kwargs):
        if not fired["done"] and matches(kwargs):
            fired["done"] = True
            on_match()
        return original(**kwargs)

    monkeypatch.setattr(client, "update_item", wrapped)
    return fired


def _matches_seal_write(kwargs: dict, *, tenant_id: str) -> bool:
    ue = kwargs.get("UpdateExpression", "") or ""
    key = kwargs.get("Key", {})
    return (
        key.get("tenant_id") == tenant_id
        and "sealed_user_dollar_base" in ue
    )


# ------------------------------------------------------- resolve_user_dollar_default


def test_resolve_base_picks_the_greatest_effective_period_at_or_before_the_query(
    tenants_table,
):
    """I3: resolution = the `user_dollar_defaults` entry
    with the greatest key `<= period`.' Three effective periods on the row;
    querying a period strictly between two of them must resolve to the
    EARLIER one, not the later (future) one and not the tenant's whole
    history collapsed to a single figure."""
    from dynamo.tenants import TenantsRepository, resolve_user_dollar_default

    _seed_tenant_row(
        tenants_table, "resolve-order-tenant",
        user_dollar_defaults={
            "2026-01": Decimal(10_000_000),
            "2026-06": Decimal(20_000_000),
            "2027-01": Decimal(30_000_000),
        },
        user_dollar_defaults_version=1,
    )
    assert resolve_user_dollar_default(TenantsRepository().get("resolve-order-tenant"), "2026-08") == 20_000_000
    assert resolve_user_dollar_default(TenantsRepository().get("resolve-order-tenant"), "2026-01") == 10_000_000
    assert resolve_user_dollar_default(TenantsRepository().get("resolve-order-tenant"), "2026-12") == 20_000_000
    assert resolve_user_dollar_default(TenantsRepository().get("resolve-order-tenant"), "2027-06") == 30_000_000


def test_resolve_base_is_none_before_the_earliest_effective_period(tenants_table):
    """A period strictly before every entry's effective date has no base in
    force yet -- distinct from the tenant having no history at all (the next
    test), but the observable answer is the same: unconfigured for THIS
    period."""
    from dynamo.tenants import TenantsRepository, resolve_user_dollar_default

    _seed_tenant_row(
        tenants_table, "resolve-too-early-tenant",
        user_dollar_defaults={"2027-01": Decimal(30_000_000)},
        user_dollar_defaults_version=1,
    )
    assert resolve_user_dollar_default(TenantsRepository().get("resolve-too-early-tenant"), "2026-01") is None


def test_resolve_base_is_none_for_a_tenant_with_no_default_ever_set(tenants_table):
    """Priority case: a tenant row exists (created, active) but has NEVER had
    `user_dollar_defaults` written -- the map attribute itself is absent, not
    merely empty. I3: 'An empty history means unconfigured, not an error.'
    `resolve_base` must return `None` here rather than raising KeyError/
    TypeError on the missing attribute -- a raise would make this wall's
    admission path unusable for every tenant that has never touched it."""
    from dynamo.tenants import TenantsRepository, resolve_user_dollar_default

    _seed_tenant_row(tenants_table, "no-default-ever-tenant")
    assert resolve_user_dollar_default(TenantsRepository().get("no-default-ever-tenant"), "2026-09") is None


# --------------------------------------------------------------------------- sealing: the unconfigured case


def test_sealing_an_unconfigured_tenant_writes_nothing_and_returns_none(tenants_table):
    """Priority case, I3's own words: 'Raising here is the natural thing to
    write and would make every admission for a tenant with no default retry
    forever.' A tenant with no `user_dollar_defaults` history at all must seal
    to `None` (the wall's 'not configured' signal) WITHOUT writing a
    `sealed_user_dollar_base` entry -- a stray sealed entry for an
    unconfigured tenant would later resolve as a real (if degenerate) ceiling
    for a wall the operator never opted into.

    MUTATION CHECK (reasoned, not run against real code, since none exists
    yet): a sealing function that instead raised `KeyError`/`TypeError` on the
    missing map would turn this into a 500 on every admission for every
    tenant that has not configured the wall -- the overwhelming majority on
    day one of the feature shipping."""
    from dynamo.tenants import TenantsRepository

    _seed_tenant_row(tenants_table, "seal-unconfigured-tenant")
    result = TenantsRepository().seal_user_dollar_base("seal-unconfigured-tenant", "2026-09")
    assert result is None

    row = _read_row(tenants_table, "seal-unconfigured-tenant")
    sealed = row.get("sealed_user_dollar_base") or {}
    assert "2026-09" not in sealed


def test_sealing_is_idempotent_once_a_period_is_already_sealed(tenants_table):
    """I3/P3.3: 'the first admission needing period P's base fixes it.' Every
    LATER admission for the same (tenant, period) must observe the SAME
    sealed value without attempting to overwrite it -- the
    `attribute_not_exists(sealed_user_dollar_base.#p)` half of the condition.
    Sealing a period that is already sealed must return the EXISTING sealed
    value, not the tenant's current (possibly since-changed) default -- G6
    ('a sealed period's base cannot change') would be violated by a sealer
    that read the live default and returned it instead of the frozen one."""
    from dynamo.tenants import TenantsRepository

    _seed_tenant_row(
        tenants_table, "seal-idempotent-tenant",
        user_dollar_defaults={"2026-01": Decimal(50_000_000)},
        sealed_user_dollar_base={"2026-09": Decimal(50_000_000)},
        user_dollar_defaults_version=1,
    )
    result = TenantsRepository().seal_user_dollar_base("seal-idempotent-tenant", "2026-09")
    assert result == 50_000_000

    row = _read_row(tenants_table, "seal-idempotent-tenant")
    assert int(row["sealed_user_dollar_base"]["2026-09"]) == 50_000_000


# --------------------------------------------------------------------------- G4: every admission agrees


def test_sealing_a_fresh_configured_tenant_freezes_the_resolved_value(tenants_table):
    """G4: 'Every member of a tenant in one period is admitted against the
    same base.' The first-ever seal of a period must write EXACTLY the value
    `resolve_base` would have returned for it at that moment, and the sealed
    map must carry the sealed period's own key (not some other period's, and
    not the whole map echoed back)."""
    from dynamo.tenants import TenantsRepository

    _seed_tenant_row(
        tenants_table, "seal-fresh-tenant",
        user_dollar_defaults={"2026-01": Decimal(42_000_000)},
        user_dollar_defaults_version=1,
    )
    result = TenantsRepository().seal_user_dollar_base("seal-fresh-tenant", "2026-09")
    assert result == 42_000_000

    row = _read_row(tenants_table, "seal-fresh-tenant")
    assert int(row["sealed_user_dollar_base"]["2026-09"]) == 42_000_000


# --------------------------------------------------------------------------- the priority-1 headline: the lost-update race


def test_seal_version_clause_makes_a_concurrent_setter_win_the_admissions_seal(
    monkeypatch, tenants_table,
):
    """THE test I3's own worked example exists to make executable.

    I3, verbatim: 'an admission resolves 100 for P; the setter writes 80 for P
    and its `attribute_not_exists(seal.P)` condition succeeds, returning
    success to the operator; the admission then seals 100. Every admission
    agrees, and the operator was told a base took effect that never will.'
    That is the failure mode WITHOUT the `user_dollar_defaults_version = :v_read`
    clause. This test pins the CORRECT behaviour WITH it: the admission's
    seal attempt, built from its stale read, must FAIL on the version clause
    once the setter's write has landed, forcing a re-read that sees the
    setter's value -- and the seal that finally commits must carry THAT
    value, not the one the admission originally resolved.

    The race, concretely (P = "2027-01", chosen far enough in the future that
    the setter's own "no effective period at or before today" cheap check
    does not confound this test with an unrelated refusal):
      - seeded: `user_dollar_defaults = {"2026-01": $100}`, version=1 -- so
        `resolve_base(tenant, "2027-01")` is $100 before the race.
      - intercepted: the SEALER's own conditional `update_item` (matched on
        `sealed_user_dollar_base` appearing in its UpdateExpression) is about
        to fire, built from `v_read=1`.
      - in that window, the REAL setter runs: `set_user_dollar_default`
        writes a NEW effective period "2026-10" (in the future relative to
        real "now", so the setter's own cheap check does not refuse it) at
        $80, and bumps `user_dollar_defaults_version` to 2. Since "2026-10" is
        the new greatest key `<=` "2027-01", `resolve_base(tenant, "2027-01")`
        is now $80.
      - the sealer's intercepted call then proceeds with its STALE
        `v_read=1` -- which must now fail the `user_dollar_defaults_version =
        :v_read` clause, since the row is at version 2.

    ASSERTING THE SEALED VALUE, NOT MERELY THAT A RETRY HAPPENED: a sealer
    with NO version clause at all (the bug I3 describes) would sail through
    on `attribute_not_exists(seal.P)` alone on its FIRST attempt, seal $100,
    and never retry -- this test would then see `result == 100_000_000` and
    `sealed_user_dollar_base["2027-01"] == 100_000_000`. Only a sealer that
    (a) has the version clause AND (b) actually re-reads and retries on
    failure lands here with $80, which is the assertion below.
    """
    from dynamo.tenants import TenantsRepository

    tenant_id = "seal-race-tenant"
    _seed_tenant_row(
        tenants_table, tenant_id,
        user_dollar_defaults={"2026-01": Decimal(100_000_000)},
        user_dollar_defaults_version=1,
    )
    repo = TenantsRepository()

    def _setter_races_in():
        TenantsRepository().set_user_dollar_default(
            tenant_id=tenant_id, effective_period="2026-10", amount_microusd=80_000_000,
        )

    fired = _intercept_one_update_item(
        monkeypatch, repo,
        matches=lambda kwargs: _matches_seal_write(kwargs, tenant_id=tenant_id),
        on_match=_setter_races_in,
    )

    result = TenantsRepository().seal_user_dollar_base(tenant_id, "2027-01")

    assert fired["done"], "fixture sanity: the concurrent setter write did not actually race in"
    assert result == 80_000_000, (
        "the seal landed the ADMISSION's stale read (100) instead of the "
        "SETTER's value (80) -- the version clause did not do its job, or "
        "the sealer did not retry after losing it"
    )
    row = _read_row(tenants_table, tenant_id)
    assert int(row["sealed_user_dollar_base"]["2027-01"]) == 80_000_000
    # The setter's write is the one that must be visible in the defaults map
    # too -- the race must not have been silently dropped on either side.
    assert int(row["user_dollar_defaults"]["2026-10"]) == 80_000_000
    assert int(row["user_dollar_defaults_version"]) == 2


# --------------------------------------------------------------------------- the setter: sealed vs unsealed future period


def test_setter_refuses_a_sealed_period_and_leaves_the_row_untouched(tenants_table):
    """Priority case: 'a setter's write against a sealed period (refused)...'
    I3: 'The two directions are symmetric: a setter cannot change a sealed
    period, and a sealer cannot freeze a history that moved under it.' G6: 'A
    sealed period's base cannot change.' `effective_period="2026-10"` is
    chosen strictly in the future relative to real "now" (2026-09), so a
    refusal here is UNAMBIGUOUSLY the seal guard
    (`attribute_not_exists(sealed_user_dollar_base.#p)`), not the setter's
    separate 'no past/current effective period' cheap check -- the two
    refusal reasons this test must not conflate."""
    from dynamo.tenants import TenantsRepository

    tenant_id = "setter-vs-sealed-tenant"
    _seed_tenant_row(
        tenants_table, tenant_id,
        user_dollar_defaults={"2026-01": Decimal(10_000_000)},
        sealed_user_dollar_base={"2026-10": Decimal(10_000_000)},
        user_dollar_defaults_version=1,
    )
    repo = TenantsRepository()

    with pytest.raises(Exception) as ei:  # noqa: PT011 -- exact type unspecified by I3, see module docstring
        repo.set_user_dollar_default(
            tenant_id=tenant_id, effective_period="2026-10", amount_microusd=99_000_000,
        )
    # An `AttributeError` here means the method does not exist at all (the
    # pre-implementation state on origin/main) -- that is a DIFFERENT failure
    # than the refusal this test asserts, and must not be mistaken for it: a
    # broad `pytest.raises(Exception)` around a call to a missing method
    # would otherwise pass VACUOUSLY before the feature is even implemented.
    assert not isinstance(ei.value, AttributeError), (
        f"set_user_dollar_default does not exist yet ({ei.value!r}) -- this "
        f"is the module not being implemented, not the sealed-period refusal "
        f"this test is pinning"
    )

    row = _read_row(tenants_table, tenant_id)
    assert "2026-10" not in row.get("user_dollar_defaults", {})
    assert int(row["user_dollar_defaults_version"]) == 1, (
        "a refused setter write must not bump the version either -- a bumped "
        "version on a refused write would let a LATER, unrelated write's own "
        "version compare-and-set land against a version number no successful "
        "write ever produced"
    )


def test_setter_accepts_an_unsealed_future_period(tenants_table):
    """The other half of the symmetric pair: the SAME tenant, a DIFFERENT
    (unsealed) future period, must succeed -- proving the refusal above is
    really about that one period's seal and not, say, a fixture mistake that
    would have refused everything."""
    from dynamo.tenants import TenantsRepository

    tenant_id = "setter-vs-unsealed-tenant"
    _seed_tenant_row(
        tenants_table, tenant_id,
        user_dollar_defaults={"2026-01": Decimal(10_000_000)},
        sealed_user_dollar_base={"2026-10": Decimal(10_000_000)},
        user_dollar_defaults_version=1,
    )
    repo = TenantsRepository()

    repo.set_user_dollar_default(
        tenant_id=tenant_id, effective_period="2026-11", amount_microusd=99_000_000,
    )

    row = _read_row(tenants_table, tenant_id)
    assert int(row["user_dollar_defaults"]["2026-11"]) == 99_000_000
    assert int(row["user_dollar_defaults_version"]) == 2
    # The sealed period from the fixture is untouched by an unrelated setter
    # write elsewhere in the map.
    assert int(row["sealed_user_dollar_base"]["2026-10"]) == 10_000_000


def test_setter_refuses_an_effective_period_at_or_before_its_own_current_period(
    tenants_table,
):
    """Secondary case (not the sealed-period guard): I3's 'cheap first check'
    -- 'refuses an effective period at or before its own current period.' Real
    'now' is used (this check is stated as reading the clock, unlike
    `resolve_base`/the seal, which take an explicit period and touch no
    clock), so this test seeds no fixture beyond an empty tenant row and
    tries to set a default effective THIS period."""
    import datetime

    from dynamo.tenants import TenantsRepository

    tenant_id = "setter-cheap-check-tenant"
    _seed_tenant_row(tenants_table, tenant_id)
    repo = TenantsRepository()
    today = datetime.datetime.now(datetime.timezone.utc)
    this_period = f"{today.year:04d}-{today.month:02d}"

    with pytest.raises(Exception) as ei:  # noqa: PT011 -- exact type unspecified by I3
        repo.set_user_dollar_default(
            tenant_id=tenant_id, effective_period=this_period, amount_microusd=1,
        )
    assert not isinstance(ei.value, AttributeError), (
        f"set_user_dollar_default does not exist yet ({ei.value!r}) -- not "
        f"the cheap-check refusal this test is pinning"
    )

    row = _read_row(tenants_table, tenant_id)
    assert this_period not in row.get("user_dollar_defaults", {})


# --------------------------------------------------- what moto let through
class TestNoEmptyExpressionAttributeNames:
    """Real DynamoDB rejects `ExpressionAttributeNames={}`; moto accepts it.

    This is not a hypothetical, and it was the SEAL path specifically. Both writes on
    the tenant row build their alias map conditionally — an alias exists only for a
    nested path or a pruned period — and on the seal's first write for a tenant there is
    neither, so the map came out empty. Every unit test here passed and the service
    answered `ValidationException: ExpressionAttributeNames must not be empty`, which
    meant the wall never engaged for any tenant at all: the first seal is the one every
    tenant hits. Found in the real-machine phase, which is the only place it was visible.

    The setter is checked the same way and did NOT have the defect — in the scenario
    below it always carries an alias. Its test is kept because the two paths build that
    map the same way and the next edit to either could introduce it; a check that only
    covers the path that broke is a check that has to be rewritten to catch the sibling.
    """

    def _capture(self, monkeypatch, repo):
        seen = []
        real = repo._table.update_item

        def _spy(**kwargs):
            seen.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(repo._table, "update_item", _spy)
        return seen

    def test_the_first_default_for_a_tenant_passes_no_empty_alias_map(
        self, tenants_table, monkeypatch,
    ):
        from dynamo.tenants import TenantsRepository
        repo = TenantsRepository()
        _seed_tenant_row(tenants_table, "empty-names-setter")
        seen = self._capture(monkeypatch, repo)
        repo.set_user_dollar_default(tenant_id="empty-names-setter",
                                     effective_period="2099-03",
                                     amount_microusd=5_000_000)
        assert seen, "the setter issued no update at all"
        for kwargs in seen:
            assert kwargs.get("ExpressionAttributeNames", {"x": "y"}) != {}, (
                "ExpressionAttributeNames was passed as an empty map. moto accepts "
                "that and real DynamoDB refuses it with a ValidationException, so this "
                "would pass here and fail in production on the first write for every "
                f"tenant. kwargs={kwargs!r}"
            )

    def test_the_first_seal_for_a_tenant_passes_no_empty_alias_map(
        self, tenants_table, monkeypatch,
    ):
        from dynamo.tenants import TenantsRepository
        repo = TenantsRepository()
        _seed_tenant_row(tenants_table, "empty-names-seal",
                         user_dollar_defaults={"2026-01": Decimal(9_000_000)},
                         user_dollar_defaults_version=1)
        seen = self._capture(monkeypatch, repo)
        sealed = repo.seal_user_dollar_base("empty-names-seal", "2026-09")
        assert sealed == 9_000_000, f"the seal did not return the resolved base: {sealed}"
        assert seen, "the seal issued no update at all"
        for kwargs in seen:
            assert kwargs.get("ExpressionAttributeNames", {"x": "y"}) != {}, (
                "the defect this class exists for: an empty alias map on the path "
                "every tenant takes exactly once. "
                f"every tenant hits exactly once. kwargs={kwargs!r}"
            )
