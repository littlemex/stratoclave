"""The durable rung of the ladder, against a real DynamoDB shape (moto).

`mvp.pricing` already keeps the last table a source returned in memory, so a blip
does not drop a task to the bundled floor. What it cannot survive is a restart: a
deploy, a scale-out or an OOM lands a fresh task with no memory of prices that were
read an hour ago, and the floor is a document from whenever the release was cut.

Everything here is about that gap, and about failing downward: an unreadable,
unwritable or unrecognised snapshot means the rung is skipped, never that charging
stops.
"""
from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from mvp.pricing_feeds.snapshot import (
    DEFAULT_STALE_AFTER_SECONDS,
    SCHEMA_VERSION,
    Snapshot,
    SnapshotStore,
    digest_of,
)
from mvp.rates import Rate

_RATES = {
    "opus": Rate(5_500_000, 27_500_000, 550_000, 6_875_000),
    "haiku": Rate(1_100_000, 5_500_000, 110_000, 1_375_000),
}


def test_round_trip(dynamodb_mock):
    store = SnapshotStore()
    assert store.load() is None, "nothing stored yet is a valid state, not an error"
    stored = store.save(_RATES, {"opus": "bedrock-agreement(input,output)"})
    assert stored is not None
    read = store.load()
    assert read is not None
    assert read.rates == _RATES
    assert read.provenance["opus"] == "bedrock-agreement(input,output)"
    assert read.digest == digest_of(_RATES)
    assert read.fetched_at > 0


def test_an_unchanged_table_cuts_no_new_version(dynamodb_mock):
    """The property that keeps hourly polling from becoming a pile of daily rows: the
    version id is the digest of the table, so re-reading the same prices is a no-op.
    "How many versions exist" then answers "how many times prices moved"."""
    store = SnapshotStore()
    store.save(_RATES, {}, now=1_000_000.0)
    for _ in range(5):
        store.save(_RATES, {}, now=1_000_060.0)
    versions = store.versions()
    assert len(versions) == 1, versions
    assert versions[0]["version"] == digest_of(_RATES)


def test_a_changed_price_cuts_a_version_and_moves_the_pointer(dynamodb_mock):
    store = SnapshotStore()
    store.save(_RATES, {}, now=1_000_000.0)
    moved = dict(_RATES, opus=Rate(6_000_000, 27_500_000, 550_000, 6_875_000))
    store.save(moved, {}, now=1_000_100.0)
    versions = store.versions()
    assert {v["version"] for v in versions} == {digest_of(_RATES), digest_of(moved)}
    current = store.load()
    assert current is not None and current.digest == digest_of(moved)
    # The superseded version is still readable, which is what makes a recompute at the
    # table that WAS in force possible rather than a reconstruction.
    old = store.load_version(digest_of(_RATES))
    assert old is not None and old.rates == _RATES


def test_first_seen_survives_a_confirmation_of_unchanged_prices(dynamodb_mock):
    """A price that has not moved for a month is a month old and still current. The
    version keeps the date it was FIRST seen; confirmation only refreshes staleness."""
    store = SnapshotStore()
    store.save(_RATES, {}, now=1_000_000.0)
    store.save(_RATES, {}, now=1_000_000.0 + 20 * 3600)
    read = store.load()
    assert read is not None
    assert read.fetched_at == 1_000_000.0
    assert read.last_seen_at >= 1_000_000.0


def test_an_empty_table_is_refused(dynamodb_mock):
    """"The feed returned nothing" is the state this store exists to survive."""
    store = SnapshotStore()
    store.save(_RATES, {})
    with capture_logs() as logs:
        assert store.save({}, {}) is None
    assert any(e.get("event") == "price_feed_snapshot_write_skipped" for e in logs)
    assert store.load().rates == _RATES        # the good table is still there


def test_a_schema_this_build_does_not_know_is_skipped(dynamodb_mock):
    store = SnapshotStore()
    store.save(_RATES, {})
    table = store._get_table()
    table.update_item(
        Key={"pk": "CONFIG#pricefeed", "sk": f"__ratefeed__{digest_of(_RATES)}"},
        UpdateExpression="SET schema_version = :v",
        ExpressionAttributeValues={":v": SCHEMA_VERSION + 1},
    )
    with capture_logs() as logs:
        assert store.load() is None
    assert any(e.get("event") == "price_feed_snapshot_schema_unknown" for e in logs)


def test_one_bad_row_does_not_discard_the_good_ones(dynamodb_mock):
    """A dropped key falls back to the floor; dropping the whole snapshot would send
    every other key there too."""
    store = SnapshotStore()
    store.save(_RATES, {})
    table = store._get_table()
    table.update_item(
        Key={"pk": "CONFIG#pricefeed", "sk": f"__ratefeed__{digest_of(_RATES)}"},
        UpdateExpression="SET #r.#k = :bad",
        ExpressionAttributeNames={"#r": "rates", "#k": "haiku"},
        ExpressionAttributeValues={":bad": {"input_per_mtok_microusd": -1}},
    )
    with capture_logs() as logs:
        read = store.load()
    assert read is not None
    assert set(read.rates) == {"opus"}
    assert any(e.get("event") == "price_feed_snapshot_row_skipped" for e in logs)


def test_a_read_failure_is_absence_not_an_exception(dynamodb_mock):
    """The store is on the path a request can reach, so a DynamoDB problem has to look
    like "no snapshot" rather than a 500."""
    store = SnapshotStore(table_name="stratoclave-does-not-exist")
    with capture_logs() as logs:
        assert store.load() is None
    assert any(e.get("event") == "price_feed_snapshot_read_failed" for e in logs)


def test_a_write_failure_is_reported_and_survivable(dynamodb_mock):
    """A store that cannot be written to is survivable: the in-memory table keeps
    serving, and the failure is named rather than swallowed."""
    store = SnapshotStore(table_name="stratoclave-does-not-exist")
    with capture_logs() as logs:
        assert store.save(_RATES, {}) is None
    assert any(e.get("event") == "price_feed_version_write_failed" for e in logs), logs


def test_a_missing_version_row_is_absence_not_an_exception(dynamodb_mock):
    """A pointer at a version that is not there — a half-failed write, a hand-edited
    table — reads as "no stored version", which lands on the floor rather than raising on
    the request path."""
    store = SnapshotStore()
    store.save(_RATES, {})
    store._get_table().delete_item(
        Key={"pk": "CONFIG#pricefeed", "sk": f"__ratefeed__{digest_of(_RATES)}"})
    with capture_logs() as logs:
        assert store.load() is None
    assert any(e.get("event") == "price_feed_version_missing" for e in logs)


def test_staleness_is_reported_but_never_expires_a_price():
    """Stale real prices beat a stale bundled floor. Expiring a snapshot would change
    the amount charged with nobody deciding to, which is the one thing this subsystem
    must not do quietly."""
    now = 1_000_000.0
    fresh = Snapshot(rates=_RATES, fetched_at=now - 60)
    old = Snapshot(rates=_RATES, fetched_at=now - DEFAULT_STALE_AFTER_SECONDS - 1)
    assert fresh.is_stale(now=now) is False
    assert old.is_stale(now=now) is True
    # Still usable: staleness is a label on the data, not a gate in front of it.
    assert old.rates == _RATES


def test_digest_changes_only_when_a_rate_changes():
    """The only change signal available: AWS publishes no end date for a promotional
    price and every Bedrock `effectiveDate` reads as the first of the current month,
    so a diff between two fetches is what a price change looks like from outside."""
    assert digest_of(_RATES) == digest_of(dict(_RATES))
    moved = dict(_RATES, opus=Rate(5_500_001, 27_500_000, 550_000, 6_875_000))
    assert digest_of(moved) != digest_of(_RATES)


def test_both_reads_are_strongly_consistent(dynamodb_mock, monkeypatch):
    """The pointer is written after the version row it names, so an eventually-consistent
    pair can show a reader the new pointer and not yet the version — and this module's
    answer to a missing version row is "no stored version", which drops the whole table to
    the floor. A read that can invent that state is not a read worth having."""
    store = SnapshotStore()
    store.save(_RATES, {})
    table = store._get_table()
    seen: list[bool] = []
    original = table.get_item

    def watched(**kwargs):
        seen.append(bool(kwargs.get("ConsistentRead")))
        return original(**kwargs)

    monkeypatch.setattr(table, "get_item", watched)
    assert store.load() is not None
    assert seen == [True, True], "both the pointer and the version must be read strongly"


def test_a_pass_that_started_earlier_cannot_repoint_current_backwards(dynamodb_mock):
    """Two passes begin from the same active version. One finishes and moves the pointer; the
    other, which began before that and is carrying older prices, must step aside.

    The fence is the version each pass STARTED from, not a timestamp and not the value read a
    microsecond before writing. A CAS against a just-read value has the same hole as no CAS
    at all — the late writer reads the winner's version and then satisfies its own condition —
    and a clock-based guard hands ordering to whichever task has the worst clock, where one
    future-dated write freezes the pointer until the world catches up.
    """
    store = SnapshotStore()
    stale = {"opus": Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)}
    fresh = {"opus": Rate(9_000_000, 9_000_000, 9_000_000, 9_000_000)}
    store.save(_RATES, {}, now=1_000_000.0, fenced_on=None)
    fence = digest_of(_RATES)                    # what both passes saw when they started

    assert store.save(fresh, {}, now=1_000_100.0, fenced_on=fence) is not None
    # The other pass, still fenced on the version it started from, has been overtaken.
    assert store.save(stale, {}, now=1_000_200.0, fenced_on=fence) is None
    current = store.load()
    assert current is not None
    assert current.rates == fresh, "a later write must not win with older prices"
    # Its version row still exists, so nothing was lost — only the pointer was defended.
    assert store.load_version(digest_of(stale)) is not None


def test_a_skewed_clock_cannot_freeze_the_pointer(dynamodb_mock):
    """Ordering must not depend on client clocks. A task whose clock is years ahead writes a
    version, and the next correct task still moves the pointer — with a timestamp guard it
    would have been locked out until wall time caught up."""
    store = SnapshotStore()
    from_the_future = {"opus": Rate(1, 1, 1, 1)}
    store.save(from_the_future, {}, now=4_000_000_000.0, fenced_on=None)
    correct = {"opus": Rate(2_000_000, 2, 2, 2)}
    stored = store.save(correct, {}, now=1_800_000_000.0,
                        fenced_on=digest_of(from_the_future))
    assert stored is not None and stored.rates == correct
