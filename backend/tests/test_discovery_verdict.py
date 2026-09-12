"""E7/E8/E9's shared foundation: the probe verdict's shape, its closed value
sets, its storage identity, and the one property that makes "invalidated by
signal, never by clock" checkable at all -- that nothing here reads a clock.

This is the most dangerous shape in the whole change (two other units read
it and are blind to how it is written), so every field, every closed value
set, and the `(profile_id, invocation)` storage identity are pinned exactly
as ratified: module `mvp.discovery.verdict`, dataclass `ProbeVerdict`
(frozen), fields `profile_id / observation_scope / invocation / verified_at /
verified_by / pricing_key_at_verification / wire_protocol_verified / state`,
`verified_by` in `{"probe", "production"}`, `state` in `{"verified",
"invalidated"}`, storage `pk = VERDICT#{profile_id}` / `sk =
INVOCATION#{invocation}`, functions `get_probe_verdict(profile_id,
invocation)` / `list_probe_verdicts()` / `put_probe_verdict(verdict)`.

**The axis is `invocation` (`"sync"` / `"stream"`), not the pricing axis's
`mode`.** `mvp/pricing_feeds/dimensions.py` already owns `MODES = ("standard",
"batch", "flex", "priority")` for pricing, and this change was originally
drafted with the verdict keyed on that SAME word before the collision was
caught -- pinned here so a second collision cannot recur silently: this file
never imports or compares against `mvp.pricing_feeds.dimensions.MODES`, and
if a future edit adds one, that is the regression this paragraph exists to
flag.

Two things this file does NOT do, on purpose. It does not assert on the raw
DynamoDB item or its literal pk/sk strings -- the frozen document names the
key SCHEME, not a promise that a test may reach around the three named
functions to read it directly, and the same posture `test_discovery_records
.py` already takes for E1's store. And it does not know which physical table
the verdict shares with unit 1's promotion candidates -- only that it is
"the same table as unit 1's candidates", never which one -- so this file
exercises the store purely through its own three functions.

`ObservationScope` is reused from `mvp.discovery.records` (E1, already
shipped) rather than re-declared here, per the frozen shape's own field type.
"""
from __future__ import annotations

import dataclasses

import pytest

from mvp.discovery.records import ObservationScope

SYNC = "sync"
STREAM = "stream"


def _scope(**overrides) -> ObservationScope:
    base = dict(
        account="776010787911", region="us-east-1",
        credentials_fingerprint="test-fingerprint", observed_at="2026-09-10T00:00:00+00:00",
    )
    base.update(overrides)
    return ObservationScope(**base)


def _verdict(profile_id: str = "us.anthropic.claude-fable-5", **overrides):
    from mvp.discovery.verdict import ProbeVerdict

    base = dict(
        profile_id=profile_id,
        observation_scope=_scope(),
        invocation=SYNC,
        verified_at="2026-09-10T00:00:00+00:00",
        verified_by="probe",
        pricing_key_at_verification="fable-global",
        wire_protocol_verified="messages",
        state="verified",
    )
    base.update(overrides)
    return ProbeVerdict(**base)


# ---------------------------------------------------------------------------
# The dataclass shape itself: every named field, frozen, closed value sets.
# ---------------------------------------------------------------------------
def test_verdict_carries_every_named_field():
    v = _verdict()
    assert v.profile_id == "us.anthropic.claude-fable-5"
    assert v.observation_scope.account == "776010787911"
    assert v.invocation == SYNC
    assert v.verified_at == "2026-09-10T00:00:00+00:00"
    assert v.verified_by == "probe"
    assert v.pricing_key_at_verification == "fable-global"
    assert v.wire_protocol_verified == "messages"
    assert v.state == "verified"


def test_verdict_is_frozen():
    v = _verdict()
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.state = "invalidated"  # type: ignore[misc]


class TestClosedValueSets:
    """"nothing else" is the frozen decision's own words for both of these
    fields. A dataclass that merely documented the two values in a comment,
    accepting any string, would pass every OTHER test in this file and only
    fail here -- so these are written as their own non-vacuous pair per
    field: the two named values construct cleanly, and a third value is
    refused."""

    @pytest.mark.parametrize("value", ["probe", "production"])
    def test_verified_by_accepts_the_two_named_values(self, value):
        assert _verdict(verified_by=value).verified_by == value

    def test_verified_by_refuses_a_third_value(self):
        with pytest.raises(ValueError):
            _verdict(verified_by="operator")

    @pytest.mark.parametrize("value", ["verified", "invalidated"])
    def test_state_accepts_the_two_named_values(self, value):
        assert _verdict(state=value).state == value

    def test_state_refuses_a_third_value(self):
        with pytest.raises(ValueError):
            _verdict(state="pending")


# ---------------------------------------------------------------------------
# The store: identity is (profile_id, invocation), not profile_id alone.
# ---------------------------------------------------------------------------
class TestStoreRoundTrip:
    def test_put_then_get_returns_what_was_stored(self, dynamodb_mock):
        from mvp.discovery.verdict import get_probe_verdict, put_probe_verdict

        v = _verdict(profile_id="us.anthropic.claude-opus-5", invocation=SYNC)
        put_probe_verdict(v)
        got = get_probe_verdict("us.anthropic.claude-opus-5", SYNC)
        assert got is not None
        assert got.state == "verified"
        assert got.pricing_key_at_verification == "fable-global"
        assert got.wire_protocol_verified == "messages"

    def test_get_on_a_profile_with_no_verdict_at_all_is_none(self, dynamodb_mock):
        from mvp.discovery.verdict import get_probe_verdict

        assert get_probe_verdict("us.anthropic.never-probed", SYNC) is None

    def test_sync_and_stream_of_the_same_profile_do_not_collide(self, dynamodb_mock):
        """The identity is `(profile_id, invocation)`, sk = `INVOCATION#
        {invocation}` -- not `profile_id` alone. Verified behaviourally:
        writing a verdict for one invocation kind must not appear when the
        OTHER kind of the SAME profile is read, and must not clobber a
        verdict already written for that other kind. A store keyed on
        `profile_id` alone would pass a single-invocation test and only fail
        here -- and this axis matters specifically because "a 200 with
        absent usage counters" (one of E9's invalidating signals) is a
        streaming-only failure shape, so sync and stream verdicts for the
        SAME profile can legitimately disagree."""
        from mvp.discovery.verdict import get_probe_verdict, put_probe_verdict

        pid = "us.anthropic.claude-two-invocations"
        put_probe_verdict(_verdict(profile_id=pid, invocation=SYNC,
                                    pricing_key_at_verification="key-sync"))
        put_probe_verdict(_verdict(profile_id=pid, invocation=STREAM,
                                    pricing_key_at_verification="key-stream"))

        sync = get_probe_verdict(pid, SYNC)
        stream = get_probe_verdict(pid, STREAM)
        assert sync is not None and sync.pricing_key_at_verification == "key-sync"
        assert stream is not None and stream.pricing_key_at_verification == "key-stream"

    def test_put_is_a_full_replace_for_the_same_profile_and_invocation(self, dynamodb_mock):
        """Re-verifying (or invalidating) the same `(profile_id, invocation)`
        must overwrite the prior verdict, not accumulate a second row beside
        it -- matching E1's own "fresh, complete snapshot" store convention
        for the same reason: a verdict is the CURRENT truth for that pair,
        not a log."""
        from mvp.discovery.verdict import get_probe_verdict, put_probe_verdict

        pid = "us.anthropic.claude-overwritten"
        put_probe_verdict(_verdict(profile_id=pid, invocation=SYNC, state="verified"))
        put_probe_verdict(_verdict(profile_id=pid, invocation=SYNC, state="invalidated"))
        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "invalidated"

    def test_list_probe_verdicts_returns_every_stored_verdict(self, dynamodb_mock):
        from mvp.discovery.verdict import list_probe_verdicts, put_probe_verdict

        put_probe_verdict(_verdict(profile_id="us.anthropic.claude-list-a", invocation=SYNC))
        put_probe_verdict(_verdict(profile_id="us.anthropic.claude-list-b", invocation=STREAM))

        seen = {(v.profile_id, v.invocation) for v in list_probe_verdicts()}
        assert ("us.anthropic.claude-list-a", SYNC) in seen
        assert ("us.anthropic.claude-list-b", STREAM) in seen


# ---------------------------------------------------------------------------
# "Current" means state == "verified". There is no clock.
# ---------------------------------------------------------------------------
class TestNoClock:
    """"There is no clock. A verdict does not expire; it is invalidated by
    signal or it stands." Pinned as an absence: a verdict planted with a
    `verified_at` far enough in the past that any expiry window a
    clock-based design might have chosen (a day, a week, a year) would
    already have lapsed must still read back exactly as stored -- `state ==
    "verified"` -- because nothing in `get_probe_verdict` or
    `list_probe_verdicts` may consult wall-clock time to decide that."""

    def test_an_old_verified_at_does_not_demote_the_read(self, dynamodb_mock):
        from mvp.discovery.verdict import get_probe_verdict, put_probe_verdict

        pid = "us.anthropic.claude-ancient"
        # Deliberately implausible if any expiry clock were consulted: five
        # years before this contract was even drafted.
        put_probe_verdict(_verdict(profile_id=pid, invocation=SYNC,
                                    verified_at="2021-01-01T00:00:00+00:00"))
        got = get_probe_verdict(pid, SYNC)
        assert got is not None
        assert got.state == "verified", (
            "a verdict must stand on its own age forever -- only a signal may "
            "move it to invalidated, never the passage of time"
        )

    def test_list_does_not_filter_old_verdicts_out_either(self, dynamodb_mock):
        from mvp.discovery.verdict import list_probe_verdicts, put_probe_verdict

        pid = "us.anthropic.claude-ancient-listed"
        put_probe_verdict(_verdict(profile_id=pid, invocation=SYNC,
                                    verified_at="2020-06-15T00:00:00+00:00"))
        found = [v for v in list_probe_verdicts() if v.profile_id == pid]
        assert len(found) == 1
        assert found[0].state == "verified"
