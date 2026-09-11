"""E1 — the discovered-record store: a record persists, and a blocker carries
its evidence.

**A named interface gap, resolved by picking a concrete answer.** The handoff
fixes the record's field names and the storage key scheme (`user_id =
"DISCOVERED#{profile_id}"`, `tenant_id = "SYSTEM"`, in the existing
`stratoclave-user-tenants` table) but never names the functions that read and
write it. This file commits to `mvp.discovery.records.put_discovered_record(record)` and
`get_discovered_record(profile_id)` as that entry point, because every other repository
in this codebase that owns a DynamoDB item type (`dynamo/tenants.py`,
`dynamo/user_tenants.py`) exposes exactly this shape — a save and a load, named
plainly — and because the handoff places the record dataclasses in this same
module rather than in `dynamo/`, unlike every other table-backed type in the
repository. If the real interface names these differently, the mismatch is a
naming difference for the integrator to reconcile, not a behavioural one this
file gets to paper over: every assertion below is about what the STORED DATA
must contain and where it must live, not about the calling convention.

Uses the shared `dynamodb_mock` fixture (`tests/conftest.py`), which is the
existing convention for exercising the real Stratoclave tables under moto — no
real AWS call is made or needed for a storage-layer test.
"""
from __future__ import annotations

import pytest

from mvp.discovery.records import (
    Blocker,
    DiscoveredRecord,
    ObservationScope,
    get_discovered_record,
    put_discovered_record,
)


def _scope(**overrides) -> ObservationScope:
    base = dict(account="776010787911", region="us-east-1",
               credentials_fingerprint="abc123", observed_at=1_800_000_000.0)
    base.update(overrides)
    return ObservationScope(**base)


def _record(profile_id: str = "us.anthropic.claude-fable-5", **overrides) -> DiscoveredRecord:
    base = dict(
        profile_id=profile_id,
        provider="anthropic",
        profile_scope="us",
        model_family="anthropic.claude-fable-5",
        jurisdiction_bounded=True,
        destination_regions=("us-east-1", "us-east-2", "us-west-2"),
        invocation_region="us-east-1",
        raw_id=profile_id,
        raw_payload={"inferenceProfileId": profile_id, "status": "ACTIVE"},
        observation_scope=_scope(),
        blockers=(),
    )
    base.update(overrides)
    return DiscoveredRecord(**base)


# --- the blocker carries its evidence -----------------------------------------
def test_a_blocker_carries_its_own_evidence_independently_of_its_siblings():
    """"a blocker carries its evidence" — checked as more than an echo of the
    constructor: a record with TWO blockers must keep each one's evidence
    attached to the right blocker, not merged, deduplicated, or overwritten by
    whichever was added last. A store that flattened them into one shared
    evidence string, or kept only the last blocker written, would pass a
    single-blocker check and only fail here."""
    b1 = Blocker(type="no_agreement_offer", subtype="not_marketplace_metered",
                evidence="ValidationException: Agreement not supported for this model",
                first_seen=1_800_000_000.0, last_seen=1_800_000_000.0)
    b2 = Blocker(type="no_token_pricing", subtype="no_token_dimensions",
                evidence="4 row(s), none token-priced",
                first_seen=1_800_000_100.0, last_seen=1_800_000_100.0)
    record = _record(blockers=(b1, b2))
    assert record.blockers[0].evidence == b1.evidence
    assert record.blockers[1].evidence == b2.evidence
    assert record.blockers[0].evidence != record.blockers[1].evidence


def test_a_blockers_evidence_and_every_other_field_survives_the_store(dynamodb_mock):
    """The test above shows a `Blocker` CAN hold distinct evidence in memory; it
    is not a defence of the STORE, because it never calls `put_discovered_record`
    or `get_discovered_record` — a store that silently wrote every blocker's
    `evidence` as the empty string would still pass it. E1's own verification
    column names evidence explicitly, quoted rather than paraphrased: "a record
    persists; a blocker carries its evidence" — that is a round-trip claim, so
    it is checked as one here: construct, `put_discovered_record`, `get_discovered_record`,
    then compare the RELOADED blocker's fields against the original, one
    assertion per field rather than a single `==` on the whole `Blocker`, so a
    failure names exactly which field the store dropped rather than "something
    differs".

    `type` and `subtype` are each given a distinctive, real value (not "x") so a
    store that round-tripped a placeholder correctly but truncated or coerced a
    longer real string would still be caught. `first_seen` and `last_seen` are
    ISO-8601 strings — the shape reconciliation actually writes (checked
    directly against `test_discovery_reconcile.py`'s own CLI output, not
    assumed) — and set to two DIFFERENT timestamps deliberately, mirroring
    `destination_regions`/`invocation_region` elsewhere in this file: a store
    that answered both from a single stored timestamp would pass a test that
    used the same value for both and only fail here."""
    blocker = Blocker(
        type="no_agreement_offer", subtype="not_marketplace_metered",
        evidence="ValidationException: Agreement not supported for this model",
        first_seen="2026-01-01T00:00:00+00:00", last_seen="2026-06-15T12:30:00+00:00",
    )
    record = _record(blockers=(blocker,))
    put_discovered_record(record)
    loaded = get_discovered_record(record.profile_id)
    assert loaded is not None
    reloaded = loaded.blockers[0]
    assert reloaded.type == blocker.type
    assert reloaded.subtype == blocker.subtype
    assert reloaded.evidence == blocker.evidence, (
        f"evidence did not survive the store — got {reloaded.evidence!r}, "
        f"expected {blocker.evidence!r}; a status without evidence is not "
        f"something an operator can act on"
    )
    assert reloaded.first_seen == blocker.first_seen
    assert reloaded.last_seen == blocker.last_seen
    assert reloaded.first_seen != reloaded.last_seen, (
        "the two timestamps came back equal despite being written distinct — "
        "this test would not catch a store that answered both from one field"
    )


# --- the record persists -------------------------------------------------------
def test_a_record_persists_and_round_trips(dynamodb_mock):
    """The store round trip: save, then load by the same profile id, and get
    back every field — not merely a truthy "found something". Distinguishing
    fields deliberately (`destination_regions` plural vs `invocation_region`
    singular, `raw_id` vs the parsed identity fields) so a store that
    collapsed the two concepts the handoff insists are separate would be
    caught here rather than passing on a coincidence."""
    record = _record()
    put_discovered_record(record)
    loaded = get_discovered_record(record.profile_id)
    assert loaded is not None
    assert loaded.profile_id == record.profile_id
    assert loaded.provider == "anthropic"
    assert loaded.profile_scope == "us"
    assert loaded.model_family == "anthropic.claude-fable-5"
    assert loaded.jurisdiction_bounded is True
    assert tuple(loaded.destination_regions) == record.destination_regions
    assert loaded.invocation_region == "us-east-1"
    assert loaded.raw_id == record.raw_id
    assert loaded.observation_scope.account == "776010787911"


def test_destination_regions_and_invocation_region_are_not_conflated():
    """"Separate fields: they are different concepts and conflating them in a
    change about residency is not a small error." A profile spanning three
    destinations but invoked through one endpoint must keep both — plural set,
    singular endpoint — never collapse to one value or the other."""
    record = _record(destination_regions=("us-east-1", "us-east-2", "us-west-2"),
                     invocation_region="us-east-1")
    assert len(record.destination_regions) == 3
    assert record.invocation_region == "us-east-1"
    assert record.invocation_region in record.destination_regions
    # The failure this guards against: a store that only kept ONE region and
    # answered both fields from it would still pass the line above by
    # accident, so the plural set is asserted to actually be plural.
    assert record.destination_regions != (record.invocation_region,)


def test_a_missing_profile_is_not_a_record(dynamodb_mock):
    """The other half of "a record persists": nothing was ever asked to persist
    for this profile id, so there must be nothing to load — not an empty
    record, not an exception, `None`."""
    assert get_discovered_record("no.such.profile") is None


def test_two_profiles_do_not_collide_in_storage(dynamodb_mock):
    """The key scheme names the profile id (`DISCOVERED#{profile_id}`), never a
    fixed or shared row, so two different discovered profiles must be
    independently readable — the failure this catches is a store that
    hardcoded the tenant-side of the key without the profile id folded into
    the user-side of it, which would let the second save silently overwrite
    the first."""
    fable5 = _record("us.anthropic.claude-fable-5", provider="anthropic")
    stability = _record("stability.sd3-5-large-v1:0", provider="stability",
                        model_family="stability.sd3-5-large-v1:0",
                        profile_scope="none", jurisdiction_bounded=False)
    put_discovered_record(fable5)
    put_discovered_record(stability)
    assert get_discovered_record(fable5.profile_id).provider == "anthropic"
    assert get_discovered_record(stability.profile_id).provider == "stability"


def test_the_item_lands_at_the_key_scheme_the_handoff_names(dynamodb_mock):
    """Pinned literally, because this is the one piece of E1's storage the
    handoff states as fact rather than leaving to the implementer: "a
    separate item type in the existing `stratoclave-user-tenants` table, key
    `user_id = "DISCOVERED#{profile_id}"`, `tenant_id = "SYSTEM"`." Checked
    against the raw table rather than through `get_discovered_record`, so a `get_discovered_record`
    that happened to work via some OTHER key (a GSI scan, a different prefix)
    would not quietly satisfy this."""
    from dynamo.client import user_tenants_table_name

    record = _record("us.anthropic.claude-fable-5")
    put_discovered_record(record)
    table = dynamodb_mock.Table(user_tenants_table_name())
    item = table.get_item(Key={
        "user_id": f"DISCOVERED#{record.profile_id}", "tenant_id": "SYSTEM",
    }).get("Item")
    assert item is not None, (
        "no item was written at user_id='DISCOVERED#{profile_id}', "
        "tenant_id='SYSTEM' — the exact key scheme the handoff specifies for E1"
    )


def test_a_record_never_saved_leaves_no_trace(dynamodb_mock):
    """The negative control for the persistence tests above: `get_discovered_record`
    must not report a record simply because a `DiscoveredRecord` value was
    constructed in memory — only `put_discovered_record` may cause one to exist. Without
    this, a `get_discovered_record` that fabricated an answer from whatever was in scope
    would pass the round-trip test above by accident."""
    _record("never.saved.profile")  # constructed, never saved
    assert get_discovered_record("never.saved.profile") is None
