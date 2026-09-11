"""The floor comparison at grant (`GrantFloorRefusal` in
`mvp/admin_entitlements.py`): a grant is refused when the rate the
gateway would actually bill disagrees, in the under-charging direction,
with the reviewed floor row for the target's registry `pricing_key`.

Written from the change's own interface description alone, split-impl
style: the code author (`mvp/admin_entitlements.py`'s new logic) is
blind to this file, and this file is written blind to their code.
`GrantFloorRefusal` and the two-string reason vocabulary do not exist
yet on `origin/main` -- every test below that needs them is expected to
be RED until that lands, not because this file is wrong.

The comparison's live side was revised after this file's first version.
That version stopped because nothing named how a discovered record
becomes a live rate reading -- verified by reading the production code
the change touches (`records.py`, `reconcile.py`, `composite.py`):
`DiscoveredRecord.raw_payload` is the inference-profile summary, not a
rate card, and the only priced `Selection` ever computed is transient
inside `composite.py::_build`, post-clamp. The comparison resolves this
by not using a `Selection` at all: it compares the bundled floor row
against `pricing.rate_for(pricing_key)` -- the SAME live read
`estimate_cost_microusd` uses and `reservation_bound.py` prices its
ceiling from -- which resolves as floor -> active source table -> admin
overrides, with no clamp at any step. Both sides are now integer
micro-USD per MTok; there is no `Decimal`/`float` conversion in the
verdict itself.

WHAT CHANGED FOR THE STUBS, AND WHAT I DID ABOUT EACH:

- **The leg-by-leg comparison is now fully constructible without
  guessing a private seam.** The precondition is: (a) a discovered
  record exists for the entry's `(model_family, profile_scope)` --
  `DiscoveredRecord` carries both fields directly, so this is seeded
  through the real, change-named
  `mvp.discovery.records.put_discovered_record`, not a guessed hook; (b)
  an admin override installs the "live" number -- through
  `dynamo.pricing_config.PricingConfigRepository.set_rates` +
  `pricing.reset_cache()`, exactly the pattern `tests/test_pricing.py::
  test_pricing_config_override_is_hot_reloaded` already uses, per the
  coordinator's own instruction to follow the existing pricing tests
  rather than monkeypatch `rate_for` itself (which would test nothing,
  since it is the function under test).

- **The two mandatory tests moved, and I corrected the moved value.**
  Both sides being integer micro-USD removes the `Decimal`-to-`float`
  hazard from the verdict, so I moved them to the refusal-fields tests'
  field-population path, below, as instructed. But I did NOT reuse
  haiku-3's `312500` (`$0.3125`) -- I checked it first: 312500 /
  1_000_000 = 5/16, and 5/16 IS exactly representable in binary
  (`Decimal(0.3125) == Decimal("0.3125")` exactly, no residual digits;
  verified by running it). The float round-trip hazard being guarded
  against (`float(0.1) is 0.100000000000000005...`) requires a value
  whose reduced fraction still carries a factor of 5 in the denominator
  after removing factors of 1_000_000 = 2^6 x 5^6 -- i.e. NOT a multiple
  of 5^6 = 15625. 312500 = 15625 x 20 IS such a multiple, so it is
  binary-EXACT and this test, built on it, would have passed against a
  float round-trip and proved nothing -- exactly the round-numbers
  failure mode this file exists to guard against. `opus`'s
  `cache_read_per_mtok_microusd = 550_000` ($0.55/MTok) is NOT a
  multiple of 15625 (549999.9999... territory) and is genuinely
  binary-inexact: `Decimal(0.55)` prints
  `0.5500000000000000444089209850062616169452667236328125`, a positive
  epsilon that `composite.py::_to_micro`'s `ROUND_CEILING` would inflate
  by one micro if it, or an equivalent, ran on a `float`-tainted value
  anywhere in the path that populates the refusal's fields. Used below
  in place of haiku-3's leg, for that reason -- flagged for the
  coordinator to confirm, since it is a substitution of VALUE, not of
  intent.

- **The zero-floor-leg skip has no remaining content, and I deleted its
  test rather than pad it -- twice.** `rate_for()` always returns a
  fully-populated `Rate` -- four `int` fields, never optional, validated
  non-negative at every layer (`mvp/rates.py::validate_rate_table`) --
  so there is no `Selection`-shaped "absent" or "widened" leg for a
  resolved `Rate` to have; the case that once needed its own refusal no
  longer reaches the comparison. I did not write a test for that case.

  What survives -- "a floor leg of `0` is not priced and is skipped" --
  I first kept a test for (`TestZeroFloorLeg`, since removed). A later
  non-vacuity sweep removed the implementation's `if floor_micro == 0:
  continue` line and the whole suite, including that test, still
  passed. I checked why rather than re-adding a weaker version of the
  same test: the comparison downstream is `live_micro < floor_micro`;
  with `floor_micro == 0` that reads `live_micro < 0`, and `live_micro`
  can never be negative -- `mvp/rates.py::validate_rate_table` rejects a
  negative rate at every layer a `Rate` can be built from (floor,
  source, admin override), with no exception. So skipping a zero leg
  and comparing it produce the SAME outcome in every reachable state,
  and the rule has no observable behaviour under this system's own
  invariants. I deleted the test rather than leave it in: it read as
  protection and provided none, passing identically against the correct
  implementation and the one line that breaks it. If a future change
  ever let a live rate go negative through some path
  `validate_rate_table` does not cover, this rule would regain a
  subject and the test would need to come back with it.

- **`floor_leg_unreadable` appears to have lost its trigger too, and I
  did not write a test for it or use it anywhere.** It was the reason
  string for an absent/widened `Selection` leg; with that leg shape
  gone, I cannot construct any scenario that would raise it under the
  new design (a floor row, once it exists, always has four legs;
  `rate_for()` always returns four legs). I did not delete it from the
  change's own two-string closed vocabulary -- that's the coordinator's
  call -- but flagging this now rather than writing a test against a
  case I cannot construct.

- **The "no discovered record" tests are unchanged from the first
  version** -- they already tested exactly what this revision does not
  touch, using the real registry rather than a copy of the claim, which
  is the pattern to keep. A later non-vacuity sweep found they do not,
  on their own, defend the `_has_discovered_record` gate: they install
  no override, so `rate_for` equals the floor and passes whether or not
  the gate runs. `TestDiscoveredRecordGateIsLoadBearing` below adds the
  missing crossing -- a disagreeing override WITH no discovered record,
  which must still pass -- plus its mirror (the same disagreement, with
  a record, which must refuse) so the pair shows the record is what
  makes the difference.

- **The common path this revision calls out is new and added below**
  (`TestNoOverrideNoSourceEqualsFloor`): with neither an admin override
  nor a live source value for a key, `rate_for` returns the floor
  itself, so the comparison is trivially equal and must pass -- not
  refuse on the temptation that "exactly equal" looks suspicious.

REMAINING OPEN QUESTIONS (flagged for the coordinator; noted here so
they travel with the file):
  1. `floor_leg_unreadable` may be an orphaned reason string (above).
  2. The refusal's `live_micro` field's exact type/unit is not re-stated
     after this revision (it predates it, when the live side was a
     `Decimal` from a priced `Selection`; now both sides are `int`
     micro-USD). The field population tests below accept either an
     `int` micro-USD value or a `Decimal`/numeric USD-per-MTok value,
     reconciled via `Decimal(str(...))` (never `Decimal(float(...))`,
     which would reintroduce the exact hazard this file is testing for)
     -- so they pin the NUMBER regardless of which unit the field turns
     out to hold, without guessing the unit itself.
  3. The existing-grant disagreement value's exact string content is
     not pinned beyond "the return type's existing `Optional[str]`
     warning channel" -- the tests below assert it is a non-`None` `str`
     distinct from `AUDIT_DROPPED_WRITE_FAILED`, not a specific message.
  4. How "has a discovered record" is decided for `(model_family,
     profile_scope)` is still not spelled by name (only
     `get_discovered_record(profile_id)` and `list_discovered_records()`
     are named). `DiscoveredRecord` carries `model_family`/
     `profile_scope` as plain fields, so seeding one with those set and
     relying on ANY reasonable existence check (a scan of
     `list_discovered_records()`, most plausibly) is the only
     construction that does not invent a private name -- flagged, not
     re-stopped on, since unlike the live-side gap above there is
     exactly one textually-grounded reading here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import pytest

TENANT = "acme-fable-tenant"

# The floor comparison's own scope, read from the REAL bundled registry
# rather than a fixture -- this is the one entry the fact is about.
REAL_FAMILY = "claude-fable-5"
REAL_SCOPE = "global"
REAL_PRICING_KEY = "fable-global"

# Registry-independent instance of the same "no discovered record" rule.
NOREC_FAMILY = "acme-widget"
NOREC_SCOPE = "us"
NOREC_PRICING_KEY = "default"

# The leg-by-leg comparison: round numbers are enough here, because the
# hazard being guarded against (direction, boundary) is not a
# units-conversion hazard -- that is the refusal-fields tests' job,
# below, with a value chosen for that specifically.
COMPARISON_FAMILY = "acme-quasar"
COMPARISON_SCOPE = "us"
COMPARISON_PRICING_KEY = "gemma"  # bundled floor: 140_000 / 400_000 / 140_000 / 175_000

# The real fleet's majority case: no override, no source value, `rate_for`
# returns the floor itself.
COMMON_FAMILY = "acme-drift"
COMMON_SCOPE = "us"
COMMON_PRICING_KEY = "nemotron"

# The zero-floor-leg skip's one remaining, narrow case: the real
# zero-cache-leg row.
VLLM_FAMILY = "acme-selfhosted"
VLLM_SCOPE = "us"
VLLM_PRICING_KEY = "vllm"

# A pricing_key that is not in the bundled floor at all.
UNREVIEWED_FAMILY = "acme-void"
UNREVIEWED_SCOPE = "us"
UNREVIEWED_PRICING_KEY = "acme-unreviewed-key-xyz"

# The corrected mandatory test: opus's cache_read leg, genuinely
# binary-inexact (see the module docstring for the divisibility check).
REFUSAL_FIELDS_FAMILY = "acme-nova"
REFUSAL_FIELDS_SCOPE = "us"
REFUSAL_FIELDS_PRICING_KEY = "opus"  # bundled floor: 5_500_000 / 27_500_000 / 550_000 / 6_875_000

# Existing-grant triple. A distinct pricing_key from the leg-by-leg
# comparison's, even though `rate_for`/`reset_cache` are already
# test-scoped -- kept separate so a reviewer never has to check whether
# the two classes' override state could interact.
EXISTING_GRANT_FAMILY = "acme-legacy"
EXISTING_GRANT_SCOPE = "us"
EXISTING_GRANT_PRICING_KEY = "sonnet-3"

# The discovered-record gate, defended directly: same disagreeing rate,
# crossed with presence/absence of a discovered record.
GATE_FAMILY = "acme-sentinel"
GATE_SCOPE = "us"
GATE_PRICING_KEY = "haiku"  # bundled floor: 1_100_000/5_500_000/110_000/1_375_000


def _fixture_registry():
    from mvp.models import ModelEntry

    def _entry(family, scope, pricing_key):
        return ModelEntry(
            provider="anthropic", bedrock_model_id=f"us.anthropic.{family}",
            bedrock_region="us-east-1", aliases=(f"{family}-{scope}",),
            wire_protocol="messages", pricing_key=pricing_key,
            model_family=family, profile_scope=scope,
            access="entitlement_required", jurisdiction_bounded=True, jurisdiction="us",
        )

    return (
        _entry(NOREC_FAMILY, NOREC_SCOPE, NOREC_PRICING_KEY),
        _entry(COMPARISON_FAMILY, COMPARISON_SCOPE, COMPARISON_PRICING_KEY),
        _entry(COMMON_FAMILY, COMMON_SCOPE, COMMON_PRICING_KEY),
        _entry(VLLM_FAMILY, VLLM_SCOPE, VLLM_PRICING_KEY),
        _entry(UNREVIEWED_FAMILY, UNREVIEWED_SCOPE, UNREVIEWED_PRICING_KEY),
        _entry(REFUSAL_FIELDS_FAMILY, REFUSAL_FIELDS_SCOPE, REFUSAL_FIELDS_PRICING_KEY),
        _entry(EXISTING_GRANT_FAMILY, EXISTING_GRANT_SCOPE, EXISTING_GRANT_PRICING_KEY),
        _entry(GATE_FAMILY, GATE_SCOPE, GATE_PRICING_KEY),
    )


@dataclass
class _Actor:
    """Matches `mvp.deps.AuthenticatedUser`'s fields `grant_entitlement` reads
    (`actor.user_id`, `actor.email`) -- built directly rather than importing the
    real dataclass, the same way `test_entitlement_store.py::_AdminUser` does."""

    user_id: str = "admin-1"
    email: str = "admin@example.com"
    org_id: str = "ops"
    roles: list = field(default_factory=lambda: ["admin"])
    auth_kind: str = "jwt"
    key_scopes: Optional[list] = None


def _seed_discovered_record(model_family: str, profile_scope: str, *, profile_id: str = None):
    """A real `DiscoveredRecord`, written through the real, change-named
    `put_discovered_record` -- not a private hook. `model_family`/
    `profile_scope` are the two fields the floor comparison's existence
    gate has to be reading (they are the only ones a `(model_family,
    profile_scope)` lookup could use; see the module docstring's open
    question #4). `raw_payload` content is irrelevant to the comparison
    -- it is inference-profile metadata, never a rate card -- so a
    minimal placeholder is enough."""
    from mvp.discovery.records import DiscoveredRecord, ObservationScope, put_discovered_record

    pid = profile_id or f"us.anthropic.{model_family}"
    record = DiscoveredRecord(
        profile_id=pid, provider="anthropic", profile_scope=profile_scope,
        model_family=model_family, jurisdiction_bounded=True,
        destination_regions=("us-east-1",), invocation_region="us-east-1",
        raw_id=pid, raw_payload={"inferenceProfileId": pid},
        observation_scope=ObservationScope(
            account="123456789012", region="us-east-1",
            credentials_fingerprint="test-fingerprint", observed_at="2026-09-11T00:00:00+00:00",
        ),
    )
    put_discovered_record(record)


_override_version_counter = 0


def _install_override(pricing_key: str, rate):
    """The SAME pattern `tests/test_pricing.py::
    test_pricing_config_override_is_hot_reloaded` uses -- an admin override
    through the real `PricingConfigRepository`, then a forced cache reload.
    Deliberately NOT a monkeypatch of `rate_for` itself, which is the function
    under test: patching it would prove the test calls a mock, not that the
    floor comparison reads the real effective rate.

    A fresh version string on every call: `set_rates` enforces per-version
    immutability (the same convention `tests/test_pricing.py`'s own tests use
    distinct version literals for), and this helper is called more than once
    per pricing_key across this file -- including twice in the SAME test for
    the existing-grant tests' agreement-then-disagreement sequence -- so
    reusing one literal would fail the second call for a reason that has
    nothing to do with the floor comparison."""
    global _override_version_counter
    from dynamo.pricing_config import PricingConfigRepository

    from mvp import pricing

    _override_version_counter += 1
    repo = PricingConfigRepository()
    repo.set_rates(
        version=f"test-{pricing_key}-{_override_version_counter}",
        rates={pricing_key: rate},
    )
    pricing.reset_cache()


def _floor_notes(pricing_key: str) -> str:
    from mvp.price_sources import pricing_path

    doc = json.load(open(pricing_path(), encoding="utf-8"))
    return doc["rates"][pricing_key]["notes"]


def _as_micro(value) -> int:
    """Reconcile the refusal's live-value field regardless of which unit it
    turns out to hold (see open question #2): an `int` is already
    micro-USD; anything else is treated as USD-per-MTok and converted via
    `Decimal(str(...))` -- never `Decimal(float(...))`, which would
    reintroduce the exact float hazard this file exists to catch in the
    TEST itself."""
    if isinstance(value, int):
        return value
    return int(Decimal(str(value)) * Decimal(1_000_000))


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """Every test starts from built-in defaults, not another test's override
    -- the same convention `tests/test_pricing.py` uses."""
    from mvp import pricing

    pricing.reset_cache()
    yield
    pricing.reset_cache()


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("mvp.models._REGISTRY", _fixture_registry())


# ---------------------------------------------------------------------------
# The floor comparison does not apply to an entry with no discovered record,
# and the grant proceeds exactly as it did before this feature existed.
# Unchanged from this file's first version.
# ---------------------------------------------------------------------------
class TestNoDiscoveredRecordPassesUntouched:
    """Applying the comparison universally, to every entry regardless of
    whether discovery has observed it, is an available (and wrong)
    reading of the comparison's own scoping rule, and this file's first
    version read it that way. Measured consequence: at `ce32ca8`, exactly
    one registry entry can be a grant target (`access=entitlement_required`)
    and zero discovered records exist anywhere (`--apply` has never run
    against production), so a universal reading refuses the ONLY model
    this feature could ever grant, on every deploy, forever. The
    comparison is scoped instead to an entry that HAS a discovered
    record; without one, the grant proceeds untouched."""

    def test_the_real_grantable_entry_has_no_discovered_record_today(self):
        """Pins this scoping rule's own measurement against the real
        bundled registry, not a copy of the claim."""
        from mvp.models import registry_entries

        grantable = [e for e in registry_entries() if e.access == "entitlement_required"]
        assert len(grantable) == 1, (
            f"exactly one entitlement_required entry was measured at "
            f"`ce32ca8`; found {len(grantable)}: "
            f"{[(e.model_family, e.profile_scope) for e in grantable]}."
        )
        entry = grantable[0]
        assert (entry.model_family, entry.profile_scope, entry.pricing_key) == (
            REAL_FAMILY, REAL_SCOPE, REAL_PRICING_KEY,
        ), f"the measured grantable entry no longer matches: {entry!r}"

    def test_granting_the_real_entry_succeeds_with_no_discovered_record(self, dynamodb_mock):
        from mvp.admin_entitlements import grant_entitlement

        entitlement, audit_dropped = grant_entitlement(
            tenant_id=TENANT, model_family=REAL_FAMILY, profile_scope=REAL_SCOPE,
            actor=_Actor(),
        )
        assert entitlement.model_family == REAL_FAMILY
        assert entitlement.profile_scope == REAL_SCOPE
        assert entitlement.tenant_id == TENANT
        assert audit_dropped is None

    def test_granting_a_fixture_entry_with_no_discovered_record_also_succeeds(self, dynamodb_mock, registry):
        from mvp.admin_entitlements import grant_entitlement

        first, first_dropped = grant_entitlement(
            tenant_id=TENANT, model_family=NOREC_FAMILY, profile_scope=NOREC_SCOPE,
            actor=_Actor(),
        )
        assert first.model_family == NOREC_FAMILY
        assert first_dropped is None

        second, second_dropped = grant_entitlement(
            tenant_id=TENANT, model_family=NOREC_FAMILY, profile_scope=NOREC_SCOPE,
            actor=_Actor(),
        )
        assert second.granted_at == first.granted_at, (
            "idempotency (already established before the floor comparison "
            "existed) must survive the new no-record check rather than the "
            "check racing a second write"
        )
        assert second_dropped is None


# ---------------------------------------------------------------------------
# The discovered-record gate, defended directly. Every other test that
# grants an entry with no discovered record installs no override (so
# `rate_for` == floor, equal, passes regardless of whether the gate runs),
# and every test that installs a disagreeing override also seeds a
# discovered record (so removing the gate changes nothing for it, since the
# record was already there). Neither shape can tell "the gate let this
# through" apart from "there was nothing to refuse" or "the record made it
# apply". This class crosses the two: a disagreeing override with NO
# discovered record, which is the exact production state this gate exists
# for -- measured, the one entry that can be a grant target today has no
# discovered record -- and the only construction where the gate's removal
# is observable.
# ---------------------------------------------------------------------------
class TestDiscoveredRecordGateIsLoadBearing:
    def test_disagreeing_rate_with_no_discovered_record_still_passes(self, dynamodb_mock, registry):
        """The critical case. A live rate installed BELOW the floor, on an
        entry with no discovered record: the grant must still succeed. If
        `_has_discovered_record`'s gate were removed, the comparison would
        run here too and this would refuse instead -- which is precisely
        the regression this gate exists to prevent, made concrete: the one
        real entry that can be a grant target today looks exactly like this
        fixture."""
        from mvp import pricing
        from mvp.admin_entitlements import grant_entitlement
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[GATE_PRICING_KEY]
        below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd,
            floor.cache_write_per_mtok_microusd - 1,
        )
        _install_override(GATE_PRICING_KEY, below)
        # Deliberately NOT seeding a discovered record -- that absence is the
        # whole point of this test.

        entitlement, dropped = grant_entitlement(
            tenant_id=TENANT, model_family=GATE_FAMILY, profile_scope=GATE_SCOPE,
            actor=_Actor(),
        )
        assert entitlement.model_family == GATE_FAMILY
        assert dropped is None

    def test_the_same_disagreement_with_a_discovered_record_refuses(self, dynamodb_mock, registry):
        """The mirror, so the pair shows the discovered record -- not the
        rate, not the entry -- is what makes the difference. Identical setup
        to the test above, with ONLY a discovered record added: this must
        refuse. Side by side, the two prove the gate is load-bearing rather
        than merely re-proving the leg-by-leg below-refuses rule (already
        covered by `TestLegByLegFloorComparison::test_below_refuses`) a
        second time."""
        from mvp import pricing
        from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[GATE_PRICING_KEY]
        below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd,
            floor.cache_write_per_mtok_microusd - 1,
        )
        _install_override(GATE_PRICING_KEY, below)
        _seed_discovered_record(GATE_FAMILY, GATE_SCOPE)

        with pytest.raises(GrantFloorRefusal) as exc_info:
            grant_entitlement(
                tenant_id=TENANT, model_family=GATE_FAMILY, profile_scope=GATE_SCOPE,
                actor=_Actor(),
            )
        assert exc_info.value.reason == "floor_disagreement"


# ---------------------------------------------------------------------------
# The common path: no override, no source value -> rate_for returns the
# floor itself -> equal -> the comparison must pass, not treat equality as
# suspicious.
# ---------------------------------------------------------------------------
class TestNoOverrideNoSourceEqualsFloor:
    def test_rate_for_with_no_override_equals_the_floor(self, dynamodb_mock, registry):
        """Sanity precondition, asserted directly: with no admin override and
        no live source registered, `pricing.rate_for` already returns exactly
        the bundled floor row. If this stops being true the behavioural test
        below would be passing for the wrong reason."""
        from mvp import pricing

        assert pricing.rate_for(COMMON_PRICING_KEY) == pricing.baseline_rates()[COMMON_PRICING_KEY]

    def test_grant_passes_when_rate_for_equals_the_floor_with_no_source(self, dynamodb_mock, registry):
        """When no override and no source value exist for a key, `rate_for`
        returns the floor itself, the comparison is equal, and the grant
        passes. Measured against the real account, this is the COMMON case
        (the live pass priced only 3 of the fleet's keys) -- the tempting
        wrong implementation treats exact equality as suspicious and
        refuses on it; it must not."""
        from mvp.admin_entitlements import grant_entitlement

        _seed_discovered_record(COMMON_FAMILY, COMMON_SCOPE)
        entitlement, dropped = grant_entitlement(
            tenant_id=TENANT, model_family=COMMON_FAMILY, profile_scope=COMMON_SCOPE,
            actor=_Actor(),
        )
        assert entitlement.model_family == COMMON_FAMILY
        assert dropped is None


# ---------------------------------------------------------------------------
# The leg-by-leg comparison: floor vs `pricing.rate_for(pricing_key)`, plain
# integer comparison, for an entry that has a discovered record.
# ---------------------------------------------------------------------------
class TestLegByLegFloorComparison:
    def test_equal_passes(self, dynamodb_mock, registry):
        """A live leg exactly at its floor leg passes -- through an
        EXPLICIT admin override equal to the floor (not merely absent, which
        `TestNoOverrideNoSourceEqualsFloor` already covers), so this exercises
        the comparison itself rather than the trivial no-source default."""
        from mvp import pricing
        from mvp.admin_entitlements import grant_entitlement

        floor = pricing.baseline_rates()[COMPARISON_PRICING_KEY]
        _install_override(COMPARISON_PRICING_KEY, floor)
        _seed_discovered_record(COMPARISON_FAMILY, COMPARISON_SCOPE)

        entitlement, dropped = grant_entitlement(
            tenant_id=TENANT, model_family=COMPARISON_FAMILY, profile_scope=COMPARISON_SCOPE,
            actor=_Actor(),
        )
        assert entitlement.model_family == COMPARISON_FAMILY
        assert dropped is None

    def test_above_passes(self, dynamodb_mock, registry):
        """A live leg above its floor leg -- the human's number is not the
        provider's dearest reading -- passes. Charging more than the floor
        is never the direction this refusal exists to catch."""
        from mvp import pricing
        from mvp.admin_entitlements import grant_entitlement
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[COMPARISON_PRICING_KEY]
        above = Rate(
            floor.input_per_mtok_microusd + 10_000,
            floor.output_per_mtok_microusd, floor.cache_read_per_mtok_microusd,
            floor.cache_write_per_mtok_microusd,
        )
        _install_override(COMPARISON_PRICING_KEY, above)
        _seed_discovered_record(COMPARISON_FAMILY, COMPARISON_SCOPE)

        entitlement, dropped = grant_entitlement(
            tenant_id=TENANT, model_family=COMPARISON_FAMILY, profile_scope=COMPARISON_SCOPE,
            actor=_Actor(),
        )
        assert entitlement.model_family == COMPARISON_FAMILY
        assert dropped is None

    def test_below_refuses(self, dynamodb_mock, registry):
        """A live leg below its floor leg refuses -- the whole reason this
        comparison exists: the existing `composite.py::_complete` clamp
        only fires when a pass admits incompleteness, and stays silent on
        exactly this case; a confident under-reading reaches `rate_for`
        intact and must be caught here, because grant is the last place a
        human is present before it becomes a wrong charge.

        Paired with `test_above_passes` above (same leg, same magnitude,
        opposite direction), this is also the verification plan's own
        non-vacuity requirement for the comparison's DIRECTION: an
        implementation with the comparison reversed would flip exactly one
        of this pair from red to green incorrectly."""
        from mvp import pricing
        from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[COMPARISON_PRICING_KEY]
        below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd,
            floor.cache_write_per_mtok_microusd - 1,
        )
        _install_override(COMPARISON_PRICING_KEY, below)
        _seed_discovered_record(COMPARISON_FAMILY, COMPARISON_SCOPE)

        with pytest.raises(GrantFloorRefusal) as exc_info:
            grant_entitlement(
                tenant_id=TENANT, model_family=COMPARISON_FAMILY, profile_scope=COMPARISON_SCOPE,
                actor=_Actor(),
            )
        exc = exc_info.value
        assert exc.reason == "floor_disagreement"
        assert exc.pricing_key == COMPARISON_PRICING_KEY
        assert "cache_write" in exc.leg, f"leg should name the cache_write leg, got {exc.leg!r}"
        assert exc.floor_micro == floor.cache_write_per_mtok_microusd


# ---------------------------------------------------------------------------
# The zero-floor-leg skip: deliberately NOT tested. See the module docstring
# for why this rule has no observable behaviour under the system's own
# invariants, and why a test for it was written and then deleted rather than
# kept as a green check that passes against both the correct implementation
# and the one line that would break it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The floor-row-existence check: unchanged by the move to `rate_for`, since
# it never depended on `Selection` in the first place.
# ---------------------------------------------------------------------------
class TestUnreviewedFloorRow:
    def test_pricing_key_has_no_bundled_floor_row(self):
        """Non-vacuity precondition, asserted directly against the real
        bundled floor: if this pricing_key were ever added to
        `defaults/pricing.json`, the behavioural test below would silently
        stop testing the missing-row case and start testing the leg-by-leg
        comparison instead."""
        from mvp import pricing

        assert UNREVIEWED_PRICING_KEY not in pricing.baseline_rates()

    def test_missing_floor_row_refuses_with_its_own_reason(self, dynamodb_mock, registry):
        """An entry whose registry `pricing_key` has no row in the bundled
        floor refuses with `floor_row_unreviewed`, distinct from
        `floor_disagreement` -- an unreviewed price is not a price
        disagreement, and the operator action differs (add a reviewed row,
        versus work out which of two readings is wrong)."""
        from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement

        _seed_discovered_record(UNREVIEWED_FAMILY, UNREVIEWED_SCOPE)
        with pytest.raises(GrantFloorRefusal) as exc_info:
            grant_entitlement(
                tenant_id=TENANT, model_family=UNREVIEWED_FAMILY, profile_scope=UNREVIEWED_SCOPE,
                actor=_Actor(),
            )
        exc = exc_info.value
        assert exc.reason == "floor_row_unreviewed"
        assert exc.pricing_key == UNREVIEWED_PRICING_KEY


# ---------------------------------------------------------------------------
# Every refusal's fields, including the corrected mandatory binary-inexact
# case (see module docstring for why the value moved off haiku-3's leg).
# ---------------------------------------------------------------------------
class TestRefusalFields:
    def test_opus_cache_read_leg_is_genuinely_binary_inexact(self):
        """Guards the correction itself: fails loudly if the bundled floor
        ever changes `opus.cache_read_per_mtok_microusd` away from a value
        that actually exercises the float hazard, so the mandatory test below
        cannot silently regress into 'proves nothing' the way the original
        haiku-3 example does."""
        from mvp import pricing

        leg = pricing.baseline_rates()["opus"].cache_read_per_mtok_microusd
        assert leg % 15625 != 0, (
            f"opus.cache_read_per_mtok_microusd={leg} is now a multiple of "
            f"15625 (2^6 x 5^6 / 1_000_000's remaining factor), i.e. binary-"
            f"EXACT -- pick a different leg for the mandatory float-hazard test"
        )
        # And the exact epsilon direction the hazard needs: a float
        # round-trip must land ABOVE the true decimal value (ROUND_CEILING
        # only inflates on a positive excess).
        from decimal import Decimal
        usd = Decimal(leg) / Decimal(1_000_000)
        assert Decimal(float(usd)) > usd, (
            "this leg's float round-trip does not overshoot; it would not "
            "exercise ROUND_CEILING's inflation hazard"
        )

    def test_disagreement_below_by_one_micro_on_a_binary_inexact_leg_refuses_and_displays_exactly(
        self, dynamodb_mock, registry,
    ):
        """MANDATORY (moved from the verdict path once both sides of the
        comparison became integers -- see module docstring): a live leg
        genuinely binary-inexact in decimal
        (`opus.cache_read_per_mtok_microusd = 550_000`, i.e. $0.55/MTok, NOT
        representable exactly in binary floating point) must still refuse when
        it is below its floor by the smallest possible margin -- one
        micro-USD -- and the refusal's displayed numbers must name the EXACT
        integers, not a value inflated by a stray `float` round-trip
        somewhere in the path that populates `GrantFloorRefusal`'s fields.
        This is the corrected replacement for BOTH of the original two
        mandatory tests (the two-denomination-agreement case and the
        sub-micro case), neither of which has a subject on the now-all-integer
        verdict path."""
        from mvp import pricing
        from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[REFUSAL_FIELDS_PRICING_KEY]
        one_micro_below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd - 1,
            floor.cache_write_per_mtok_microusd,
        )
        _install_override(REFUSAL_FIELDS_PRICING_KEY, one_micro_below)
        _seed_discovered_record(REFUSAL_FIELDS_FAMILY, REFUSAL_FIELDS_SCOPE)

        with pytest.raises(GrantFloorRefusal) as exc_info:
            grant_entitlement(
                tenant_id=TENANT, model_family=REFUSAL_FIELDS_FAMILY, profile_scope=REFUSAL_FIELDS_SCOPE,
                actor=_Actor(),
            )
        exc = exc_info.value
        assert exc.reason == "floor_disagreement"
        assert exc.pricing_key == REFUSAL_FIELDS_PRICING_KEY
        assert "cache_read" in exc.leg, f"leg should name cache_read, got {exc.leg!r}"
        assert exc.floor_micro == floor.cache_read_per_mtok_microusd, (
            "floor_micro must be the exact bundled integer, 550_000 -- not "
            "inflated or deflated by any conversion on the way to display"
        )
        live_value = _as_micro(exc.live_micro)
        assert live_value == floor.cache_read_per_mtok_microusd - 1, (
            f"the displayed live value must be exactly "
            f"{floor.cache_read_per_mtok_microusd - 1} micro-USD/MTok, not "
            f"{live_value} -- a float round-trip on this leg's genuinely "
            f"binary-inexact decimal value would inflate it under "
            f"ROUND_CEILING, which is exactly what this test exists to catch"
        )

    def test_each_closed_reason_populates_pricing_key_and_reason(self, dynamodb_mock, registry):
        """Every refusal -- whichever of the two reachable reasons fired
        (see module docstring on why `floor_leg_unreadable` could not be
        exercised) -- carries `reason` and `pricing_key` populated. Kept
        separate from the two scenario-specific tests above so this fact is
        pinned once, generically, rather than only implied by their asserts."""
        from mvp.admin_entitlements import GrantFloorRefusal

        assert issubclass(GrantFloorRefusal, Exception)
        from mvp.admin_entitlements import EntitlementError

        assert issubclass(GrantFloorRefusal, EntitlementError), (
            "GrantFloorRefusal's own requirement: it subclasses "
            "EntitlementError, so a refused TARGET is a 400, never a 503"
        )

    def test_floor_disagreement_notes_match_the_bundled_floor_rows_prose(self, dynamodb_mock, registry):
        """The floor row's `notes` prose travels with a `floor_disagreement`
        refusal -- the only mitigation for a floor row keyed by a shared
        name that nothing binds to the right provider model, so it has to
        be the human-readable text an operator would actually recognise,
        not merely a non-empty placeholder."""
        from mvp import pricing
        from mvp.admin_entitlements import GrantFloorRefusal, grant_entitlement
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[COMPARISON_PRICING_KEY]
        below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd - 1,
        )
        _install_override(COMPARISON_PRICING_KEY, below)
        _seed_discovered_record(COMPARISON_FAMILY, COMPARISON_SCOPE)

        with pytest.raises(GrantFloorRefusal) as exc_info:
            grant_entitlement(
                tenant_id=TENANT, model_family=COMPARISON_FAMILY, profile_scope=COMPARISON_SCOPE,
                actor=_Actor(),
            )
        assert exc_info.value.notes == _floor_notes(COMPARISON_PRICING_KEY)


# ---------------------------------------------------------------------------
# The existing-grant path -- fully constructible: install agreement, grant,
# then move the override to a disagreement and grant the SAME triple again.
# ---------------------------------------------------------------------------
class TestExistingGrant:
    def test_existing_grant_with_disagreement_returns_existing_row_and_warns(
        self, dynamodb_mock, registry, monkeypatch,
    ):
        """A grant for a triple that already exists, now disagreeing with
        the floor, returns `(existing_row, disagreement)` -- never a refusal
        (refusing an existing grant would report 'blocked' about a tenant
        already being served) and never a second write or audit event.

        This single test is the verification plan's non-vacuity check for
        BOTH of this path's wrong orderings at once: asserting no exception
        is raised rules out 'refuse instead of warn', and asserting the
        second return value is not `None` rules out 'short-circuit before
        comparing' (which would look identical to the agreement companion
        test below)."""
        from mvp import pricing
        from mvp.admin_entitlements import (
            AUDIT_DROPPED_WRITE_FAILED,
            grant_entitlement,
        )
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[EXISTING_GRANT_PRICING_KEY]
        _install_override(EXISTING_GRANT_PRICING_KEY, floor)  # agreement, for the first grant
        _seed_discovered_record(EXISTING_GRANT_FAMILY, EXISTING_GRANT_SCOPE)

        first, first_dropped = grant_entitlement(
            tenant_id=TENANT, model_family=EXISTING_GRANT_FAMILY, profile_scope=EXISTING_GRANT_SCOPE,
            actor=_Actor(),
        )
        assert first_dropped is None

        audit_calls = []
        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event",
                             lambda **kw: audit_calls.append(kw))

        disagreement = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd - 1,
        )
        _install_override(EXISTING_GRANT_PRICING_KEY, disagreement)

        second, second_signal = grant_entitlement(
            tenant_id=TENANT, model_family=EXISTING_GRANT_FAMILY, profile_scope=EXISTING_GRANT_SCOPE,
            actor=_Actor(),
        )
        assert second.granted_at == first.granted_at, "no second write"
        assert second.granted_by == first.granted_by, "no second write"
        assert second_signal is not None, (
            "the disagreement must surface through the warning channel, not "
            "be silently dropped"
        )
        assert isinstance(second_signal, str)
        assert second_signal != AUDIT_DROPPED_WRITE_FAILED, (
            "the disagreement signal must be distinguishable from the "
            "unrelated audit-write-failure sentinel that already uses this "
            "same channel"
        )
        assert audit_calls == [], "no second audit event for an existing grant"

    def test_existing_grant_with_agreement_returns_existing_row_and_none(
        self, dynamodb_mock, registry, monkeypatch,
    ):
        """The non-vacuous companion to the test above: the SAME
        existing-grant call with AGREEMENT returns `(existing_row, None)`
        -- proves the disagreement test's non-`None` second value is caused
        by the disagreement specifically, not by every existing-grant call
        carrying something in that slot regardless."""
        from mvp import pricing
        from mvp.admin_entitlements import grant_entitlement

        floor = pricing.baseline_rates()[EXISTING_GRANT_PRICING_KEY]
        _install_override(EXISTING_GRANT_PRICING_KEY, floor)
        _seed_discovered_record(EXISTING_GRANT_FAMILY, EXISTING_GRANT_SCOPE)

        first, _ = grant_entitlement(
            tenant_id=TENANT, model_family=EXISTING_GRANT_FAMILY, profile_scope=EXISTING_GRANT_SCOPE,
            actor=_Actor(),
        )

        audit_calls = []
        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event",
                             lambda **kw: audit_calls.append(kw))

        # Re-install the SAME (agreeing) rate under a fresh version, so the
        # second call is genuinely a repeat read of an agreeing live rate, not
        # a no-op skip of the override machinery.
        _install_override(EXISTING_GRANT_PRICING_KEY, floor)

        second, second_signal = grant_entitlement(
            tenant_id=TENANT, model_family=EXISTING_GRANT_FAMILY, profile_scope=EXISTING_GRANT_SCOPE,
            actor=_Actor(),
        )
        assert second.granted_at == first.granted_at
        assert second_signal is None
        assert audit_calls == []


# ---------------------------------------------------------------------------
# "A refusal writes nothing" -- verification plan, explicit and named: asserted
# directly against the store and the audit trail, not inferred from idempotency.
# ---------------------------------------------------------------------------
class TestRefusalWritesNothing:
    def test_a_refusal_writes_no_entitlement_row_and_audits_nothing(
        self, dynamodb_mock, registry, monkeypatch,
    ):
        """Revision 1's plan tested only that a second SUCCESSFUL grant is
        idempotent, which an implementation that writes-then-refuses would
        still pass. This asserts directly against the real store
        (`get_entitlement`) and the real audit call site, for a triple that
        has NEVER been granted before, so there is nothing for idempotency to
        be silently covering for."""
        from mvp import pricing
        from mvp.admin_entitlements import (
            GrantFloorRefusal,
            get_entitlement,
            grant_entitlement,
        )
        from mvp.rates import Rate

        floor = pricing.baseline_rates()[COMPARISON_PRICING_KEY]
        below = Rate(
            floor.input_per_mtok_microusd, floor.output_per_mtok_microusd,
            floor.cache_read_per_mtok_microusd, floor.cache_write_per_mtok_microusd - 1,
        )
        _install_override(COMPARISON_PRICING_KEY, below)
        _seed_discovered_record(COMPARISON_FAMILY, COMPARISON_SCOPE)

        audit_calls = []
        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event",
                             lambda **kw: audit_calls.append(kw))

        with pytest.raises(GrantFloorRefusal):
            grant_entitlement(
                tenant_id=TENANT, model_family=COMPARISON_FAMILY, profile_scope=COMPARISON_SCOPE,
                actor=_Actor(),
            )

        assert get_entitlement(TENANT, COMPARISON_FAMILY, COMPARISON_SCOPE) is None, (
            "a refusal must not have written an entitlement row"
        )
        assert audit_calls == [], "a refusal must not have audited anything"
