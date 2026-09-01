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

import json
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from mvp.models import ModelEntry
from mvp.pricing_feeds.base import FeedResult
from mvp.pricing_feeds.composite import LivePriceSource
from mvp.pricing_feeds.dimensions import RateDimension
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
    stored = store.save(_RATES, {"opus": "bedrock-agreement(input,output)"}, fenced_on=None)
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
    store.save(_RATES, {}, now=1_000_000.0, fenced_on=None)
    for _ in range(5):
        store.save(_RATES, {}, now=1_000_060.0, fenced_on=digest_of(_RATES))
    versions = store.versions()
    assert len(versions) == 1, versions
    assert versions[0]["version"] == digest_of(_RATES)


def test_a_changed_price_cuts_a_version_and_moves_the_pointer(dynamodb_mock):
    store = SnapshotStore()
    store.save(_RATES, {}, now=1_000_000.0, fenced_on=None)
    moved = dict(_RATES, opus=Rate(6_000_000, 27_500_000, 550_000, 6_875_000))
    store.save(moved, {}, now=1_000_100.0, fenced_on=digest_of(_RATES))
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
    store.save(_RATES, {}, now=1_000_000.0, fenced_on=None)
    store.save(_RATES, {}, now=1_000_000.0 + 20 * 3600, fenced_on=digest_of(_RATES))
    read = store.load()
    assert read is not None
    assert read.fetched_at == 1_000_000.0
    assert read.last_seen_at >= 1_000_000.0


def test_an_empty_table_is_refused(dynamodb_mock):
    """"The feed returned nothing" is the state this store exists to survive."""
    store = SnapshotStore()
    store.save(_RATES, {}, fenced_on=None)
    with capture_logs() as logs:
        assert store.save({}, {}, fenced_on=digest_of(_RATES)) is None
    assert any(e.get("event") == "price_feed_snapshot_write_skipped" for e in logs)
    assert store.load().rates == _RATES        # the good table is still there


def test_a_schema_this_build_does_not_know_is_skipped(dynamodb_mock):
    store = SnapshotStore()
    store.save(_RATES, {}, fenced_on=None)
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
    store.save(_RATES, {}, fenced_on=None)
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
        assert store.save(_RATES, {}, fenced_on=None) is None
    assert any(e.get("event") == "price_feed_version_write_failed" for e in logs), logs


def test_a_missing_version_row_is_absence_not_an_exception(dynamodb_mock):
    """A pointer at a version that is not there — a half-failed write, a hand-edited
    table — reads as "no stored version", which lands on the floor rather than raising on
    the request path."""
    store = SnapshotStore()
    store.save(_RATES, {}, fenced_on=None)
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
    store.save(_RATES, {}, fenced_on=None)
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


# ---------------------------------------------------------------------------
# M1, M8, M12, Q6, Q9, Q10, Q12 — the store's interface per the price-feeds
# CONTRACT (docs, not the current implementation). Most of these fail on
# `c39be4c`; each docstring says which finding it is proof against.
# ---------------------------------------------------------------------------


def _entry(model_id: str, pricing_key: str, region: str = "us-east-1") -> ModelEntry:
    # `responses` so `_candidate_regions` uses the entry's own region rather than the
    # deployment's failover policy, which these tests never configure.
    return ModelEntry(provider="anthropic", bedrock_model_id=model_id,
                     bedrock_region=region, aliases=(model_id,),
                     wire_protocol="responses", pricing_key=pricing_key)


def _card(input_usd: str, output_usd: str, cache_read: str = "1",
         cache_write: str = "2") -> dict:
    return {
        (None, RateDimension("input", "geo")): Decimal(input_usd),
        (None, RateDimension("output", "geo")): Decimal(output_usd),
        (None, RateDimension("cache_read", "geo")): Decimal(cache_read),
        (None, RateDimension("cache_write", "geo")): Decimal(cache_write),
    }


class _MutableFeed:
    """A feed whose cards a test can change between passes, so two process-local
    sources can be driven through separate fetches without real concurrency."""

    name = "mutable"

    def __init__(self, cards: dict) -> None:
        self.cards = cards

    def fetch(self, request):
        result = FeedResult()
        for model_id, card in self.cards.items():
            if model_id in request.model_ids:
                result.cards[model_id] = card
        return result


class _FencedOnRecorder:
    """Delegates every call to a real `SnapshotStore`, recording the `fenced_on` each
    caller passed to `save()`. The one fact M1 needs observed at the production call
    site, without reimplementing the store."""

    def __init__(self, inner: SnapshotStore) -> None:
        self._inner = inner
        self.fenced_on_calls: list[object] = []

    def load(self):
        return self._inner.load()

    def load_version(self, version, pointer=None):
        return self._inner.load_version(version, pointer=pointer)

    def versions(self, limit=50):
        return self._inner.versions(limit)

    def save(self, rates, provenance, live_classes=None, **kwargs):
        self.fenced_on_calls.append(kwargs.get("fenced_on", "<omitted>"))
        return self._inner.save(rates, provenance, live_classes, **kwargs)


def test_m1_composite_never_passes_the_fence_it_started_from(dynamodb_mock):
    """M1: `fenced_on` is required by the interface and must equal the version the
    pass started from. `LivePriceSource._maybe_persist` calls `store.save(...)` with no
    `fenced_on` argument at all today — the hole fix #17 was supposed to have closed,
    reopened at the one call site that matters."""
    store = SnapshotStore()
    initial = {"opus": Rate(1_000_000, 1_000_000, 1_000_000, 1_000_000)}
    store.save(initial, {}, fenced_on=None)
    start_version = digest_of(initial)
    recorder = _FencedOnRecorder(store)
    feed = _MutableFeed({"anthropic.m": _card("2", "2", "2", "2")})
    source = LivePriceSource([feed], store=recorder,
                             registry=[_entry("us.anthropic.m", "opus")],
                             interval_seconds=0)
    source.load()
    assert recorder.fenced_on_calls, "a persisting pass must call save() at least once"
    assert recorder.fenced_on_calls[-1] == start_version, (
        "composite must fence save() on the version it started from, not omit the "
        "keyword and let the store fall back to no fence at all"
    )


def test_m1_a_pass_that_loses_the_race_does_not_move_current_off_the_winner(dynamodb_mock):
    """M1: two composite passes both start from the same stored version. One fetches
    and persists first; the other, still holding that same starting point in memory,
    persists second. With no fence threaded through, the second write is checked
    against whatever is active AT WRITE TIME — which by then is the first pass's own
    write — so it always "matches" and the later writer wins regardless of which pass
    started first. `CURRENT` must stay pointed at whoever wrote first."""
    store = SnapshotStore()
    initial = {"opus": Rate(1, 1, 1, 1)}
    store.save(initial, {}, fenced_on=None)
    entry = _entry("us.anthropic.m", "opus")

    feed_a = _MutableFeed({"anthropic.m": _card("2", "2", "2", "2")})
    feed_b = _MutableFeed({})               # empty at first: caches the start version only
    source_a = LivePriceSource([feed_a], store=store, registry=[entry], interval_seconds=0)
    source_b = LivePriceSource([feed_b], store=store, registry=[entry], interval_seconds=0)

    source_b.load()                          # loads `initial`; nothing to persist yet
    source_a.load()                          # fetches and persists first: CURRENT moves

    feed_b.cards = {"anthropic.m": _card("3", "3", "3", "3")}
    source_b.load()                          # still fenced (if fenced at all) on `initial`

    current = store.load()
    assert current is not None
    assert current.rates["opus"].input_per_mtok_microusd == 2_000_000, (
        "the pass that wrote first must not be overwritten by a pass that started "
        "from the same version and only finished second"
    )


def test_m1_the_pass_that_lost_the_fence_adopts_the_winner_on_its_next_load(dynamodb_mock):
    """M1: losing the fence at the store is not enough on its own — this process's
    OWN in-memory table still shadows the snapshot in `_merged_locked()`. Unless the
    loser also adopts the winner's table, the fence is cosmetic: the losing task goes
    on charging from the price that was just proven superseded. The interface names
    this explicitly: a pass that loses the fence sets its snapshot from the stored
    winner before the next `load()` returns."""
    store = SnapshotStore()
    initial = {"opus": Rate(1, 1, 1, 1)}
    store.save(initial, {}, fenced_on=None)
    entry = _entry("us.anthropic.m", "opus")

    feed_a = _MutableFeed({"anthropic.m": _card("2", "2", "2", "2")})
    feed_b = _MutableFeed({})
    source_a = LivePriceSource([feed_a], store=store, registry=[entry], interval_seconds=0)
    source_b = LivePriceSource([feed_b], store=store, registry=[entry], interval_seconds=0)

    source_b.load()                          # caches the start version
    source_a.load()                          # wins the race
    feed_b.cards = {"anthropic.m": _card("3", "3", "3", "3")}
    source_b.load()                          # loses the race

    feed_b.cards = {}                        # nothing new to fetch on the next pass
    served = source_b.load()
    assert served["opus"].input_per_mtok_microusd == 2_000_000, (
        "a pass that lost the fence must adopt the winner's table on its next "
        "load(), not go on serving the price it just lost the race with"
    )


class _WriteFailsStore:
    """A store whose write never lands, isolated from any particular AWS failure
    mode — `load()` answers cleanly (no log noise to contaminate `--json`), and
    `save()` always returns `None`, the one fact `--apply`'s exit code and JSON are
    actually judged against."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def load(self):
        return None

    def save(self, rates, provenance, live_classes=None, **kwargs):
        return None


def test_m8_apply_reports_failure_and_exits_nonzero_when_the_write_does_not_land(
        dynamodb_mock, monkeypatch, capsys):
    """M8: `fetch --apply` exits 0 and reports success whenever a table was fetched,
    even when the store write failed outright — exactly the state this tool exists to
    turn into a loud failure, so a task racing the feeds is never mistaken for a task
    that filled the snapshot."""
    from mvp.pricing_feeds import fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "SnapshotStore", _WriteFailsStore)
    monkeypatch.setattr(fetch_mod, "LivePriceSource", _FakeApplySource)
    code = fetch_mod.main(["--apply", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False, (
        "a write that did not land must not be reported as applied"
    )
    assert code == 2, "a failed --apply must exit non-zero, not the success code"


def test_m8_apply_json_applied_field_is_the_digest_the_store_actually_holds(
        dynamodb_mock, monkeypatch, capsys):
    """M8: `applied` is `bool(args.apply)` today, so it reads `true` whether or not
    anything was actually stored. The interface makes it the digest read back from
    the store — evidence the write landed, not a restatement of the caller's flag."""
    from mvp.pricing_feeds import fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "LivePriceSource", _FakeApplySource)
    fetch_mod.main(["--apply", "--json"])
    payload = json.loads(capsys.readouterr().out)
    current = SnapshotStore().load()
    assert current is not None
    assert payload["applied"] == current.digest == digest_of(_RATES)


class _FakeApplySource:
    """Stands in for `LivePriceSource` in the `--apply` CLI tests: skips the real
    feeds (no network in a test) but persists through whatever store `fetch.main()`
    builds — the leg `--apply`'s exit code and JSON are actually judged on."""

    def __init__(self, store=None, interval_seconds=None) -> None:
        self._store = store

    def refresh(self, budget_seconds=None):
        from mvp.pricing_feeds.composite import FetchReport

        report = FetchReport(rates=dict(_RATES),
                             provenance={k: "test" for k in _RATES})
        if self._store is not None:
            # A fresh, empty-at-first store in every test that uses this fake: the
            # cold-start fence, not "skip the fence".
            self._store.save(report.rates, report.provenance, fenced_on=None)
        return report


def test_q6_versions_past_the_limit_still_contains_the_newest(dynamodb_mock):
    """Q6: `versions(limit)` applies the limit at the DynamoDB query — which pages the
    partition in sort-key (digest) order, unrelated to time — and only sorts what
    survived that cut. Past the limit it can, and here does, omit the version that is
    actually newest. This is the only listing a dispute is answered from."""
    store = SnapshotStore()
    candidates = []
    for i in range(12):
        rates = {"opus": Rate(1_000_000 + i, 1, 1, 1)}
        candidates.append((digest_of(rates), rates))
    candidates.sort(key=lambda item: item[0])   # ascending by digest: DynamoDB's sk order
    newest_digest, newest_rates = candidates[-1]  # lexicographically last: cut by a naive Limit
    fence = None
    for i, (digest, rates) in enumerate(candidates[:-1]):
        store.save(rates, {}, now=1_000_000.0 + i, fenced_on=fence)
        fence = digest
    store.save(newest_rates, {}, now=2_000_000.0, fenced_on=fence)  # the actual newest

    versions = store.versions(limit=len(candidates) - 1)
    found = [v for v in versions if v["version"] == newest_digest]
    assert found, "the newest version must not be omitted just past the limit"
    assert versions[0]["version"] == newest_digest, (
        "newest-first must survive truncation to the limit, not just apply to "
        "whatever page happened to be fetched"
    )


def test_q9_an_unchanged_pass_past_the_half_window_still_touches_last_seen_at(
        dynamodb_mock):
    """Q9: the store's own half-window touch inside `save()` is correct in isolation,
    but `composite._maybe_persist` gates every call on the FULL staleness window, so
    in steady state `save()` — and its touch — is never reached until the stored
    version has already been stale for a day. This test asserts only the stored
    marker; the warning that reports the resulting age is another author's test."""
    store = SnapshotStore()
    now = [1_000_000.0]
    half_window = DEFAULT_STALE_AFTER_SECONDS / 2
    feed = _MutableFeed({"anthropic.m": _card("5.5", "27.5", "0.55", "6.875")})
    source = LivePriceSource([feed], store=store,
                             registry=[_entry("us.anthropic.m", "opus")],
                             interval_seconds=0, clock=lambda: now[0])
    source.load()
    first = store.load()
    assert first is not None

    now[0] += half_window + 1
    source.load()                             # same prices: unchanged, but past half-window
    second = store.load()
    assert second is not None
    assert second.last_seen_at > first.last_seen_at, (
        "an unchanged pass past the half window must still touch last_seen_at, not "
        "wait for the full staleness window before the store is even called"
    )
    assert len(store.versions()) == 1, "an unchanged table must not cut a new version"


def test_q10_a_table_name_that_resolves_to_none_does_not_raise_out_of_save_or_load(
        dynamodb_mock, monkeypatch):
    """Q10: `_get_table()` sits outside `save()`'s try, so a task whose table name
    resolves to `None` (a mis-deployed config) raises straight out of `save()` on
    every successful fetch, instead of skipping the rung the way every other failure
    here does. `load()` already survives the same failure, because its call chains
    `_get_table()` inside its own try."""
    import dynamo.client as dynamo_client

    monkeypatch.setattr(dynamo_client, "pricing_config_table_name", lambda: None)
    store = SnapshotStore()
    assert store.save(_RATES, {}, fenced_on=None) is None, (
        "a store that cannot resolve a table name must fail downward, not raise"
    )
    assert store.load() is None


def test_q12_fetch_versions_prints_digests_and_timestamps_newest_first(
        dynamodb_mock, capsys):
    """Q12: `reprice --at-version` is the documented first use of the tool, and
    nothing prints the digests it takes — an operator has to read DynamoDB by hand.
    `fetch --versions` shares Q6's fix: it pages the whole partition and prints it
    sorted, newest first."""
    from mvp.pricing_feeds import fetch as fetch_mod

    store = SnapshotStore()
    older = {"opus": Rate(1, 1, 1, 1)}
    newer = {"opus": Rate(2, 2, 2, 2)}
    store.save(older, {}, now=1_000_000.0, fenced_on=None)
    store.save(newer, {}, now=1_000_100.0, fenced_on=digest_of(older))
    capsys.readouterr()                      # discard the setup calls' own log lines

    code = fetch_mod.main(["--versions"])
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln.strip()]
    assert code == 0
    assert len(lines) >= 2, "both stored versions must be listed"
    first_fields = lines[0].split()
    assert first_fields[0] == digest_of(newer), "the newest version must print first"
    second_fields = lines[1].split()
    assert second_fields[0] == digest_of(older)


def test_m12_a_corrupt_key_serves_survivors_but_the_snapshot_admits_it_and_reprice_refuses(
        dynamodb_mock):
    """M12: `load_version` already drops one bad row and serves the rest, which is
    correct for the charging path. What it does not do is SAY so: the returned
    `Snapshot` is served under the digest of the sort key it was read from while
    actually holding less than that table names, so a dispute answered from it would
    be silently wrong. `reprice --at-version` must refuse a version it can only
    partially reconstruct rather than answer a dispute with it.

    Note: the interface says only that `Snapshot` "carries whether its content digest
    matched the version it was read under", not the field's name. This test names it
    `digest_verified`; see the report for that assumption."""
    store = SnapshotStore()
    rates = dict(_RATES)                      # {"opus": ..., "haiku": ...}
    store.save(rates, {}, fenced_on=None)
    version = digest_of(rates)
    table = store._get_table()
    table.update_item(
        Key={"pk": "CONFIG#pricefeed", "sk": f"__ratefeed__{version}"},
        UpdateExpression="SET #r.#k = :bad",
        ExpressionAttributeNames={"#r": "rates", "#k": "haiku"},
        ExpressionAttributeValues={":bad": {"input_per_mtok_microusd": -1}},
    )

    read = store.load_version(version)
    assert read is not None
    assert set(read.rates) == {"opus"}, (
        "the charging path must still be served the surviving key"
    )
    assert read.digest_verified is False, (
        "a version reconstructed from less than its own stored row must say so, "
        "rather than silently keep the sort key's digest as if it still matched"
    )

    import mvp.reprice as reprice_mod

    code = reprice_mod.main(["--tenant", "t-does-not-matter", "--period", "2026-08",
                             "--at-version", version])
    assert code == 2, (
        "a dispute tool must refuse a reconstructed version, not answer with it"
    )


class _FakeDryRunSource:
    """Stands in for `LivePriceSource` in a dry-run (`--json` without `--apply`) CLI
    test: skips the real feeds, and never calls `.save()` on whatever store
    `fetch.main()` builds for a dry run — that store is a `_ReadOnlyStore`, which has
    no `save()` to call, unlike `_FakeApplySource` above which is for `--apply` only."""

    def __init__(self, store=None, interval_seconds=None) -> None:
        pass

    def refresh(self, budget_seconds=None):
        from mvp.pricing_feeds.composite import FetchReport

        return FetchReport(rates=dict(_RATES), provenance={k: "test" for k in _RATES})


def test_q24_json_mode_has_no_stdout_preamble_when_the_store_read_fails(
        dynamodb_mock, monkeypatch, capsys):
    """Q24: `fetch --json` used to call `store.load()` before entering the
    `redirect_stdout` guard, so a log line the read emits on failure (the same one
    `test_a_read_failure_is_absence_not_an_exception` asserts on, via
    `price_feed_snapshot_read_failed`) landed on stdout ahead of the JSON payload —
    a caller parsing stdout as one document got a log line first and a parse error.
    The fix moved the read inside the guard; this is the regression test for that
    ordering: with a store whose read fails, the whole of `main(["--json"])`'s stdout
    must parse as one JSON document, with nothing in front of it."""
    from mvp.pricing_feeds import fetch as fetch_mod

    # A real store pointed at a table that is not there — the same failure mode
    # `test_a_read_failure_is_absence_not_an_exception` exercises — not a fabricated
    # double, so the log line this test guards against is the one the real read
    # failure path actually emits.
    monkeypatch.setattr(
        fetch_mod, "SnapshotStore",
        lambda *a, **k: SnapshotStore(table_name="stratoclave-does-not-exist"))
    monkeypatch.setattr(fetch_mod, "LivePriceSource", _FakeDryRunSource)

    code = fetch_mod.main(["--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)          # raises on any preamble or trailer around it
    assert code == 0
    assert payload["digest"] == digest_of(_RATES)
