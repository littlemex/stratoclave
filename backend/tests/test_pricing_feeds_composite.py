"""The ladder: a fresh fetch, then the last fetch that succeeded, then the floor.

What this file is really testing is that absence never lowers a price. Every rung of
the ladder exists for a failure mode that has to keep charging correctly — a feed
that blips, a task that restarts, a provider that renames a dimension, a model whose
cache rate nobody publishes — and in each case the previous number has to stay in
force rather than a cheaper one appearing.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import pytest
from structlog.testing import capture_logs

from mvp.models import ModelEntry
from mvp.pricing_feeds.base import FeedRequest, FeedResult
from mvp.pricing_feeds.composite import LivePriceSource
from mvp.pricing_feeds.dimensions import RateDimension
from mvp.pricing_feeds.snapshot import Snapshot
from mvp.rates import RATE_FIELDS, Rate


class _Feed:
    """A feed that returns exactly what a test hands it."""

    def __init__(self, name: str, cards: dict, *, raises: bool = False,
                 result: Optional[FeedResult] = None) -> None:
        self.name = name
        self._cards = cards
        self._raises = raises
        self._result = result
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        self.last_request = request
        if self._raises:
            raise RuntimeError("boom")
        if self._result is not None:
            return self._result
        result = FeedResult()
        for model_id, card in self._cards.items():
            if model_id in request.model_ids:
                result.cards[model_id] = card
        return result


class _Store:
    def __init__(self, snapshot: Optional[Snapshot] = None) -> None:
        self.snapshot = snapshot
        self.saves: list[dict[str, Rate]] = []

    def load(self):
        return self.snapshot

    def save(self, rates, provenance, live_classes=None):
        self.saves.append(dict(rates))
        self.snapshot = Snapshot(rates=dict(rates), provenance=dict(provenance),
                                fetched_at=1_000_000.0, digest="stored",
                                live_classes={k: frozenset(v)
                                              for k, v in (live_classes or {}).items()})
        return self.snapshot


def _entry(model_id: str, pricing_key: str, region: str = "us-east-1",
          wire: str = "responses") -> ModelEntry:
    # `responses` by default so `_candidate_regions` uses the entry's own region and
    # the test does not depend on the deployment's failover configuration.
    return ModelEntry(provider="anthropic", bedrock_model_id=model_id,
                      bedrock_region=region, aliases=(model_id,), wire_protocol=wire,
                      pricing_key=pricing_key)


def _full_card(input_usd: str, output_usd: str, cache_read: str = "1",
              cache_write: str = "2", region: Optional[str] = None) -> dict:
    return {
        (region, RateDimension("input", "geo")): Decimal(input_usd),
        (region, RateDimension("output", "geo")): Decimal(output_usd),
        (region, RateDimension("cache_read", "geo")): Decimal(cache_read),
        (region, RateDimension("cache_write", "geo")): Decimal(cache_write),
    }


def test_a_fetch_publishes_the_measured_rate_in_micro_usd():
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5", "0.55", "6.875")})
    source = LivePriceSource([feed], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    table = source.load()
    assert table["opus"] == Rate(5_500_000, 27_500_000, 550_000, 6_875_000)


def test_rounding_never_truncates_a_sub_micro_rate():
    """Truncation is a discount nobody granted, so the conversion rounds up."""
    feed = _Feed("f", {"anthropic.m": _full_card("0.0000001", "1", "1", "1")})
    source = LivePriceSource([feed], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 1


def test_two_models_on_one_key_are_charged_at_the_dearer():
    """`opus` covered Claude Opus 4.1 at $15/MTok and Opus 5 at $5.50. A shared key
    can only be the max, and the operator has to be told to split it."""
    feed = _Feed("f", {
        "anthropic.new": _full_card("5.5", "27.5"),
        "anthropic.old": _full_card("15", "75"),
    })
    source = LivePriceSource([feed], store=_Store(), registry=[
        _entry("us.anthropic.new", "opus"), _entry("us.anthropic.old", "opus"),
    ], interval_seconds=0)
    with capture_logs() as logs:
        table = source.load()
    assert table["opus"].input_per_mtok_microusd == 15_000_000
    assert any(e.get("event") == "price_feed_key_spans_prices" for e in logs), logs


def test_a_class_no_feed_prices_is_filled_from_the_snapshot_then_the_floor():
    """Nemotron publishes input and output only. Those two must go live — the floor
    had it 33x too dear — while the cache legs keep the previous number instead of
    becoming zero."""
    card = {
        (None, RateDimension("input", "geo")): Decimal("0.15"),
        (None, RateDimension("output", "geo")): Decimal("0.65"),
    }
    stored = Snapshot(rates={"nemotron": Rate(9, 9, 4_242, 5_353)}, fetched_at=1.0)
    source = LivePriceSource([_Feed("f", {"nvidia.n": card})], store=_Store(stored),
                            registry=[_entry("nvidia.n", "nemotron")],
                            interval_seconds=0)
    rate = source.load()["nemotron"]
    assert rate.input_per_mtok_microusd == 150_000
    assert rate.output_per_mtok_microusd == 650_000
    assert rate.cache_read_per_mtok_microusd == 4_242    # from the snapshot
    assert rate.cache_write_per_mtok_microusd == 5_353

    # With no snapshot the same legs come from the bundled floor, never from zero.
    from mvp.pricing import baseline_rates

    fresh = LivePriceSource([_Feed("f", {"nvidia.n": card})], store=_Store(),
                           registry=[_entry("nvidia.n", "nemotron")],
                           interval_seconds=0)
    floor = baseline_rates()["nemotron"]
    assert fresh.load()["nemotron"].cache_read_per_mtok_microusd == \
        floor.cache_read_per_mtok_microusd


def test_a_feed_that_returns_nothing_keeps_the_stored_rate():
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=1.0, digest="d")
    source = LivePriceSource([_Feed("f", {})], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    assert source.load()["opus"] == Rate(1, 2, 3, 4)


def test_a_feed_that_raises_is_contained_and_reported():
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=1.0, digest="d")
    source = LivePriceSource([_Feed("boom", {}, raises=True)], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    table = source.load()          # must not raise: charging survives a broken feed
    assert table["opus"] == Rate(1, 2, 3, 4)
    report = source.last_report()
    assert report is not None and "boom" in report.feed_errors


def test_a_key_that_stops_being_readable_keeps_its_stored_value():
    """The signature of a renamed API: reachable, answering, and no longer covering a
    key it used to cover. Nothing is mispriced — the snapshot still answers — and the
    event is the only warning that precedes the whole feed going dark."""
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4), "sonnet": Rate(5, 6, 7, 8)},
                      fetched_at=1.0, digest="d")
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5", "0.55", "6.875")})
    source = LivePriceSource([feed], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    with capture_logs() as logs:
        table = source.load()
    assert table["sonnet"] == Rate(5, 6, 7, 8)      # still served from the snapshot
    assert any(e.get("event") == "price_feed_coverage_regression" for e in logs), logs
    # ...and the store keeps it too, so the next task to start does not fall to the
    # floor for a key that was readable an hour ago.
    assert "sonnet" in source._store.snapshot.rates


def test_the_feeds_are_not_called_again_inside_the_interval():
    """The pricing cache asks a source for a table every 60 s. Two AWS APIs per
    registered model on that cadence would be a self-inflicted throttle for data that
    moves monthly."""
    now = [1_000.0]
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5")})
    source = LivePriceSource([feed], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=3600, clock=lambda: now[0])
    source.load()
    source.load()
    assert feed.calls == 1
    now[0] += 3601
    source.load()
    assert feed.calls == 2


def test_an_empty_fetch_is_never_persisted():
    """"The feed returned nothing" is exactly the state the snapshot exists to
    survive, so writing it would erase the numbers it is meant to preserve."""
    store = _Store()
    source = LivePriceSource([_Feed("f", {})], store=store,
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    source.load()
    assert store.saves == []


def test_a_changed_price_is_persisted_and_logged():
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=1_000_000.0,
                      digest="old")
    store = _Store(stored)
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5", "0.55", "6.875")})
    source = LivePriceSource([feed], store=store,
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0, clock=lambda: 1_000_100.0)
    with capture_logs() as logs:
        source.load()
    assert store.saves and store.saves[-1]["opus"].input_per_mtok_microusd == 5_500_000
    assert any(e.get("event") == "price_table_changed" for e in logs), logs


def test_a_virtual_router_pool_entry_is_never_priced():
    """A semantic-router pool entry stands for a pool rather than a model, and
    `virtual` is the only thing keeping it from becoming a charge of record."""
    virtual = ModelEntry(provider="anthropic", bedrock_model_id="pool-a",
                        bedrock_region="us-east-1", aliases=("pool-a",),
                        wire_protocol="messages", pricing_key="opus",
                        served_by="semantic-router", virtual=True, sr_pool_ref="pool-a")
    feed = _Feed("f", {"pool-a": _full_card("5.5", "27.5")})
    source = LivePriceSource([feed], store=_Store(), registry=[virtual],
                            interval_seconds=0)
    assert source.load() == {}


def test_provenance_names_the_feed_and_the_legs_it_answered_for():
    """A dispute has to be answerable: which source said this, and which legs of it
    are live versus inherited."""
    card = {
        (None, RateDimension("input", "geo")): Decimal("0.15"),
        (None, RateDimension("output", "geo")): Decimal("0.65"),
    }
    source = LivePriceSource([_Feed("bedrock-price-list", {"nvidia.n": card})],
                            store=_Store(), registry=[_entry("nvidia.n", "nemotron")],
                            interval_seconds=0)
    source.load()
    provenance = source.provenance()["nemotron"]
    assert provenance.startswith("bedrock-price-list(")
    assert "input" in provenance and "output" in provenance
    assert "cache_read" not in provenance


def test_a_model_no_feed_can_price_is_reported_not_guessed():
    source = LivePriceSource([_Feed("f", {})], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    source.load()
    report = source.last_report()
    assert report is not None
    assert report.unpriced == {"us.anthropic.m": "no feed priced this model"}


@pytest.mark.parametrize("field", RATE_FIELDS)
def test_every_published_rate_is_a_non_negative_int(field):
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5", "0.55", "6.875")})
    source = LivePriceSource([feed], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    value = getattr(source.load()["opus"], field)
    assert isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _MutableFeed:
    """A feed whose answer a test can change between passes."""

    name = "mutable"

    def __init__(self, cards: dict) -> None:
        self.cards = cards

    def fetch(self, request):
        result = FeedResult()
        for model_id, card in self.cards.items():
            if model_id in request.model_ids:
                result.cards[model_id] = card
        return result


class _UnwritableStore:
    """Reads nothing and cannot write — a missing IAM permission, or a table that is
    not there yet. Both are logged and survived, which is exactly when the in-memory
    table is the ONLY layer holding real prices."""

    def load(self):
        return None

    def save(self, rates, provenance, live_classes=None):
        return None


def test_a_leg_that_stops_being_published_is_reported_not_silently_frozen():
    """The failure the key-level regression check cannot see. The key stays present and
    charged, and its cache-write leg is re-published from the snapshot on every pass —
    so a promotional rate that ended in August would still be charged in November with
    nothing to look at. Comparing which legs a feed answered for is the only signal."""
    full = _full_card("5.5", "27.5", "0.55", "3.4375")
    feed = _MutableFeed({"anthropic.m": full})
    source = LivePriceSource([feed], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    source.refresh()
    feed.cards = {"anthropic.m": {k: v for k, v in full.items()
                                  if k[1].token_class != "cache_write"}}
    with capture_logs() as logs:
        report = source.refresh()
    assert report.leg_regressions == {"opus": ["cache_write"]}
    assert any(e.get("event") == "price_feed_leg_regression" for e in logs), logs
    # The charge itself is unchanged — the stored value still answers — which is why
    # the event is the whole point.
    assert source.load()["opus"].cache_write_per_mtok_microusd == 3_437_500


def test_a_partial_feed_outage_keeps_earlier_keys_even_with_no_store():
    """With an unwritable store the in-memory table is the only last-known-good layer,
    so replacing it on each pass would throw away rates this very process read an hour
    ago. It unions instead."""
    feed = _MutableFeed({"anthropic.a": _full_card("5.5", "27.5"),
                         "nvidia.b": _full_card("0.15", "0.65")})
    source = LivePriceSource([feed], store=_UnwritableStore(), registry=[
        _entry("us.anthropic.a", "opus"), _entry("nvidia.b", "nemotron"),
    ], interval_seconds=0)
    assert {"opus", "nemotron"} <= set(source.load())
    feed.cards = {"nvidia.b": _full_card("0.15", "0.65")}      # the other feed's models vanish
    table = source.load()
    assert "opus" in table, "a key read on an earlier pass must not disappear"
    assert table["opus"].input_per_mtok_microusd == 5_500_000


def test_the_fetch_budget_stops_a_slow_pass(monkeypatch):
    """The first fetch after a cold start with no snapshot is on the request path. An
    unbounded pass there is a stall a caller pays for, so the budget is real rather
    than a constant nobody reads."""
    import time as _time

    # `refresh()` uses the whole-table budget, since nobody is waiting on it; the
    # request path has its own, tighter one. Both are set so the test says which it
    # is exercising.
    monkeypatch.setenv("STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS", "0.05")
    monkeypatch.setenv("STRATOCLAVE_PRICE_FEED_REFRESH_BUDGET_SECONDS", "0.05")

    class _Slow:
        name = "slow"

        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, request):
            self.calls += 1
            _time.sleep(0.2)
            return FeedResult()

    slow, second = _Slow(), _Slow()
    source = LivePriceSource([slow, second], store=_UnwritableStore(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    with capture_logs() as logs:
        report = source.refresh()
    assert slow.calls == 1
    assert second.calls == 0, "the second feed must be skipped once the budget is spent"
    assert report.truncated is True
    assert any(e.get("event") == "price_feed_table_partial" for e in logs), logs


def test_a_widened_leg_is_recorded_on_the_report():
    """Priced from outside the scope the request would use. Charged (at the dearer
    number) rather than dropped, and named, because it means the routing assumption and
    the price list disagree."""
    card = {
        (None, RateDimension("input", "global")): Decimal("5"),
        (None, RateDimension("output", "global")): Decimal("25"),
        (None, RateDimension("cache_read", "geo")): Decimal("0.55"),
        (None, RateDimension("cache_write", "geo")): Decimal("6.875"),
    }
    source = LivePriceSource([_Feed("f", {"anthropic.m": card})], store=_Store(),
                            registry=[_entry("global.anthropic.m", "opus")],
                            interval_seconds=0)
    with capture_logs() as logs:
        source.load()
    report = source.last_report()
    assert report is not None and report.widened == {"opus": ["cache_read", "cache_write"]}
    assert any(e.get("event") == "price_feed_scope_widened" for e in logs), logs


def test_a_declared_billing_id_is_what_the_feeds_are_asked_about():
    """`qwen.qwen3-next-80b-a3b` is billed as `...-a3b-instruct`. The registry declares
    that, so nothing has to guess which extra segments belong to which model."""
    entry = ModelEntry(provider="qwen", bedrock_model_id="qwen.q3", bedrock_region="us-east-1",
                      aliases=("q3",), wire_protocol="responses", pricing_key="qwen",
                      price_model_id="qwen.q3-instruct")
    feed = _Feed("f", {"qwen.q3-instruct": _full_card("0.14", "1.2")})
    source = LivePriceSource([feed], store=_Store(), registry=[entry], interval_seconds=0)
    assert source.load()["qwen"].input_per_mtok_microusd == 140_000


def test_the_request_path_and_an_explicit_refresh_have_different_budgets(monkeypatch):
    """A request must not wait for a whole table; a deploy-time refresh must not give up
    on one. Measured on real Bedrock, a twenty-model pass takes ~6 s — comfortably
    inside the request budget with the agreement feed's pool, and well outside it
    without — so the two numbers are not interchangeable."""
    from mvp.pricing_feeds import composite

    monkeypatch.delenv("STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("STRATOCLAVE_PRICE_FEED_REFRESH_BUDGET_SECONDS", raising=False)
    source = LivePriceSource([], store=_Store(), registry=[], interval_seconds=0)
    assert source._budget_seconds() == composite.DEFAULT_BUDGET_SECONDS
    assert source._refresh_budget_seconds() == composite.DEFAULT_REFRESH_BUDGET_SECONDS
    assert source._refresh_budget_seconds() > source._budget_seconds()


def test_a_model_that_produced_nothing_carries_the_feed_s_own_reason():
    """"no feed priced this model" is true and useless. The feed's reason for THAT model
    is kept out of the capped error list so a burst of failures cannot push it out."""
    result = FeedResult()
    result.note_model_error("anthropic.m", "rate card produced no chargeable slot")
    for i in range(30):
        result.note_error(f"noise {i}")
    feed = _Feed("f", {}, result=result)
    source = LivePriceSource([feed], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    source.load()
    report = source.last_report()
    assert report is not None
    assert report.unpriced["us.anthropic.m"] == (
        "f: rate card produced no chargeable slot")
    assert report.errors_dropped["f"] > 0, "a capped error list must say it was capped"


def test_a_coverage_regression_is_on_the_report_not_only_in_a_log():
    """A deploy gate cannot read a log line. `--strict` exits non-zero on this, so it has
    to be a field."""
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4), "sonnet": Rate(5, 6, 7, 8)},
                      fetched_at=1.0, digest="d")
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5", "0.55", "6.875")})
    source = LivePriceSource([feed], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    source.load()
    report = source.last_report()
    assert report is not None and report.coverage_regressions == ["sonnet"]
