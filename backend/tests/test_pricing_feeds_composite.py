"""The ladder: a fresh fetch, then the last fetch that succeeded, then the floor.

What this file is really testing is that absence never lowers a price. Every rung of
the ladder exists for a failure mode that has to keep charging correctly — a feed
that blips, a task that restarts, a provider that renames a dimension, a model whose
cache rate nobody publishes — and in each case the previous number has to stay in
force rather than a cheaper one appearing.
"""
from __future__ import annotations

import json
import threading
import time as _time
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
        # What the composite fenced each save on: the digest it started from, or
        # `None` for "I read the pointer and there was none". Recorded rather than
        # enforced — fencing correctness is M1's fold, not any id in this group —
        # but a test that cares can read it back.
        self.fenced_on_calls: list[Optional[str]] = []

    def load(self):
        return self.snapshot

    def save(self, rates, provenance, live_classes=None, *, now=None, fenced_on):
        self.fenced_on_calls.append(fenced_on)
        self.saves.append(dict(rates))
        # The caller's own clock when it hands one over — real stores are timed by
        # the composite's `now=self._clock()` precisely so an injected fake clock
        # and the store's stamp cannot disagree about whether a touch window has
        # passed. Real wall time only when nobody supplies one.
        fetched_at = now if now is not None else _time.time()
        self.snapshot = Snapshot(rates=dict(rates), provenance=dict(provenance),
                                fetched_at=fetched_at, digest="stored",
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
    stored = Snapshot(rates={"nemotron": Rate(9, 9, 4_242, 5_353)}, fetched_at=1.0,
                      # Genuinely priced by a feed on an earlier pass, not
                      # floor-derived — `_fallback_leg` now reads this to decide
                      # whether a quiet leg should keep the stored number or defer
                      # to the current floor.
                      live_classes={"nemotron": frozenset(
                          {"input", "output", "cache_read", "cache_write"})})
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
                            interval_seconds=3600, clock=lambda: now[0],
                            # The interval due-check now reads the monotonic budget
                            # clock as well as wall time; drive both with the same
                            # fake so advancing simulated time actually elapses for
                            # whichever one `_due()` consults.
                            budget_clock=lambda: now[0])
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

    def save(self, rates, provenance, live_classes=None, *, now=None, fenced_on):
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


def test_a_widened_leg_cannot_replace_a_dearer_stored_leg():
    """Widening prices a leg from outside the scope asked for. That is worth doing rather
    than dropping the model — but the number it produces is not evidence that the in-scope
    rate fell, so it may raise a stored leg and never lower one."""
    card = {
        (None, RateDimension("input", "global")): Decimal("5"),
        (None, RateDimension("output", "global")): Decimal("25"),
        (None, RateDimension("cache_read", "global")): Decimal("0.5"),
        (None, RateDimension("cache_write", "global")): Decimal("6.25"),
    }
    stored = Snapshot(rates={"opus": Rate(5_500_000, 27_500_000, 550_000, 6_875_000)},
                      fetched_at=1.0, digest="d",
                      live_classes={"opus": frozenset({"input", "output", "cache_read",
                                                       "cache_write"})})
    source = LivePriceSource([_Feed("f", {"anthropic.m": card})], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    rate = source.load()["opus"]
    assert rate.input_per_mtok_microusd == 5_500_000, (
        "the geo rate this request is billed at must not be replaced by the global one"
    )
    assert rate.output_per_mtok_microusd == 27_500_000


def test_a_feed_that_calls_its_own_answer_partial_cannot_lower_that_model_s_rate():
    """The published rate is a maximum over the regions a request can reach. A feed that
    could not read one of them computed that maximum over less than the truth for the models
    it was asked about, and it says so per model."""
    result = FeedResult()
    result.cards["anthropic.m"] = _full_card("1", "1", "1", "1")
    result.truncated = True
    result.incomplete_models.add("anthropic.m")
    stored = Snapshot(rates={"opus": Rate(2_000_000, 2_000_000, 2_000_000, 2_000_000)},
                      fetched_at=1.0, digest="d",
                      # Genuinely priced on an earlier pass, not floor-derived — see
                      # the note in the nemotron test above.
                      live_classes={"opus": frozenset(
                          {"input", "output", "cache_read", "cache_write"})})
    source = LivePriceSource([_Feed("f", {}, result=result)], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 2_000_000


def test_an_unrelated_partial_feed_does_not_freeze_a_complete_key():
    """A pagination limit in an offer that prices nothing we asked about must not freeze the
    price of a model another feed read completely — a clamp that fires on unrelated trouble
    is a permanent over-charge, not a safety property."""
    unrelated = FeedResult()
    unrelated.truncated = True
    unrelated.note_error("hit the page cap in an offer we do not price from")
    complete = FeedResult()
    complete.cards["anthropic.m"] = _full_card("1", "1", "1", "1")
    stored = Snapshot(rates={"opus": Rate(2_000_000, 2_000_000, 2_000_000, 2_000_000)},
                      fetched_at=1.0, digest="d")
    source = LivePriceSource(
        [_Feed("complete", {}, result=complete), _Feed("unrelated", {}, result=unrelated)],
        store=_Store(stored), registry=[_entry("us.anthropic.m", "opus")],
        interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 1_000_000


def test_a_member_whose_card_yields_no_selection_keeps_the_key_from_dropping():
    """"A feed answered" is not "this member contributed a rate". A card that exists but
    yields no standard-tier selection leaves the key's maximum short by that member, which
    is how a $15 tier gets re-published at $5."""
    result = FeedResult()
    result.cards["anthropic.cheap"] = _full_card("5", "25", "0.5", "6.25")
    # Present, answered, and unusable: output only, so no selection can be made.
    result.cards["anthropic.dear"] = {
        (None, RateDimension("output", "geo")): Decimal("75"),
    }
    stored = Snapshot(rates={"opus": Rate(15_000_000, 75_000_000, 1_500_000, 18_750_000)},
                      fetched_at=1.0, digest="d",
                      # Genuinely priced on an earlier pass, not floor-derived.
                      live_classes={"opus": frozenset(
                          {"input", "output", "cache_read", "cache_write"})})
    source = LivePriceSource([_Feed("f", {}, result=result)], store=_Store(stored), registry=[
        _entry("us.anthropic.cheap", "opus"), _entry("us.anthropic.dear", "opus"),
    ], interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 15_000_000


def test_a_shared_key_missing_its_dearer_member_keeps_the_stored_rate():
    """A key's rate is the maximum over the models that share it. If only the cheap member
    answered, the maximum is over one model instead of two — which is how a key that covers
    a $15/MTok model gets re-published at $5."""
    stored = Snapshot(rates={"opus": Rate(15_000_000, 75_000_000, 1_500_000, 18_750_000)},
                      fetched_at=1.0, digest="d",
                      # Genuinely priced on an earlier pass, not floor-derived.
                      live_classes={"opus": frozenset(
                          {"input", "output", "cache_read", "cache_write"})})
    feed = _Feed("f", {"anthropic.cheap": _full_card("5", "25", "0.5", "6.25")})
    source = LivePriceSource([feed], store=_Store(stored), registry=[
        _entry("us.anthropic.cheap", "opus"), _entry("us.anthropic.dear", "opus"),
    ], interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 15_000_000


def test_a_complete_pass_may_lower_a_rate():
    """The clamp is about incompleteness, not about refusing good news: a pass that saw
    every member and every region publishes the drop. Claude Sonnet 5 listing below Sonnet
    4.6 is a real example — a gateway that could never lower a rate would over-charge it
    forever."""
    stored = Snapshot(rates={"sonnet": Rate(3_300_000, 16_500_000, 330_000, 4_125_000)},
                      fetched_at=1.0, digest="d")
    feed = _Feed("f", {"anthropic.s5": _full_card("2.2", "11", "0.22", "2.75")})
    source = LivePriceSource([feed], store=_Store(stored),
                            registry=[_entry("us.anthropic.s5", "sonnet")],
                            interval_seconds=0)
    assert source.load()["sonnet"].input_per_mtok_microusd == 2_200_000


def test_the_region_set_is_read_once_per_pass(monkeypatch):
    """Read twice, a routing config that recovers mid-pass turns an incomplete regional
    catalogue into a "complete" answer — priced at the maximum over a smaller set than the
    truth, which is the one way this source can publish a rate that is too low."""
    from mvp.pricing_feeds import composite as mod

    calls = {"n": 0}

    def flaky(entry):
        calls["n"] += 1
        return None if calls["n"] == 1 else frozenset({"us-east-1", "us-west-2"})

    monkeypatch.setattr(mod, "_candidate_regions", flaky)
    stored = Snapshot(rates={"opus": Rate(2_000_000, 2, 2, 2)}, fetched_at=1.0, digest="d")
    feed = _Feed("f", {"anthropic.m": _full_card("1", "1", "1", "1", region="us-east-1")})
    source = LivePriceSource([feed], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus", wire="messages")],
                            interval_seconds=0)
    table = source.load()
    assert table["opus"].input_per_mtok_microusd == 2_000_000
    assert calls["n"] == 1, "the region policy must be read once per pass, not per use"


# ----------------------------------------------------------------------------------
# Group A2 additions: M2, M3, M7, M13, Q1, Q2, Q4, Q7, Q8, Q15, Q22.
# ----------------------------------------------------------------------------------


def test_a_price_list_outage_does_not_freeze_a_claude_key_the_agreement_feed_priced():
    """M2: `PriceListFeed` marking every requested model incomplete on a total
    `get_products` failure must not freeze a key another feed read completely.
    Agreement reads the Claude card in full; the Price List raises on every region
    and, today, that blanket incompleteness is unioned across feeds rather than
    attributed to the one that actually failed — so the over-charge from the stale
    floor never lifts even though the agreement card answered fully."""
    agreement = FeedResult()
    agreement.cards["anthropic.m"] = _full_card("5.5", "27.5", "0.55", "6.875")
    price_list = FeedResult()
    price_list.truncated = True
    price_list.incomplete_models.update({"anthropic.m"})
    price_list.note_error("get_products failed in every region: AccessDeniedException")
    stored = Snapshot(rates={"opus": Rate(15_000_000, 75_000_000, 1_500_000, 18_750_000)},
                      fetched_at=1.0, digest="d")
    source = LivePriceSource(
        [_Feed("bedrock-agreement", {}, result=agreement),
         _Feed("bedrock-price-list", {}, result=price_list)],
        store=_Store(stored), registry=[_entry("us.anthropic.m", "opus")],
        interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 5_500_000, (
        "a feed with no business pricing Claude must not freeze a key the agreement "
        "feed read in full"
    )


def test_a_model_the_outage_could_still_have_priced_stays_frozen():
    """M2's residual case, paired with the test above: a model no feed has
    established is out of scope for it, and the one feed that could have priced it
    is the one that just failed, still owes an answer — so unlike the Claude key
    above, it must stay at the stored rate rather than fall through on a partial
    read."""
    price_list = FeedResult()
    price_list.truncated = True
    price_list.incomplete_models.update({"nvidia.n"})
    price_list.note_error("get_products failed in every region: AccessDeniedException")
    stored = Snapshot(rates={"nemotron": Rate(150_000, 650_000, 4_242, 5_353)},
                      fetched_at=1.0, digest="d")
    source = LivePriceSource([_Feed("bedrock-price-list", {}, result=price_list)],
                            store=_Store(stored), registry=[_entry("nvidia.n", "nemotron")],
                            interval_seconds=0)
    assert source.load()["nemotron"] == Rate(150_000, 650_000, 4_242, 5_353)


def test_a_model_every_feed_calls_out_of_scope_does_not_freeze_its_key():
    """Q4: `out_of_scope` is written by three feeds and read by none, so the fold has
    no way to tell "will never answer" from "may still owe an answer" — which is the
    residual case M2 leaves. Two models share `opus`: one every feed has established
    is not its business, one a feed priced completely and fresh. The retired model's
    permanent silence must not hold the key at its old, dearer price forever."""
    retired_everywhere = FeedResult()
    retired_everywhere.out_of_scope.add("anthropic.retired")
    priced = FeedResult()
    priced.out_of_scope.add("anthropic.retired")
    priced.cards["anthropic.m"] = _full_card("5.5", "27.5", "0.55", "6.875")
    stored = Snapshot(rates={"opus": Rate(15_000_000, 75_000_000, 1_500_000, 18_750_000)},
                      fetched_at=1.0, digest="d")
    source = LivePriceSource(
        [_Feed("agreement", {}, result=retired_everywhere),
         _Feed("price-list", {}, result=priced)],
        store=_Store(stored), registry=[
            _entry("us.anthropic.retired", "opus"), _entry("us.anthropic.m", "opus"),
        ], interval_seconds=0)
    assert source.load()["opus"].input_per_mtok_microusd == 5_500_000, (
        "a sibling every feed has established out of scope must not freeze a key "
        "priced fresh and completely by the model that shares it"
    )


def test_a_blocked_save_does_not_stall_a_concurrent_load():
    """M3: the store's DynamoDB I/O runs under the source lock today, so a slow write
    stalls every concurrent reader — including one that only needs the table already
    in memory. Real threads, not a mock asserting call order: one thread's `load()`
    triggers a fetch whose `save()` blocks on an event; a second thread's `load()`,
    started while the first is blocked inside that `save()`, must return promptly
    with the table the first thread already produced."""

    class _BlockingStore:
        def __init__(self, snapshot: Snapshot) -> None:
            self.snapshot = snapshot
            self.save_entered = threading.Event()
            self.release = threading.Event()

        def load(self):
            return self.snapshot

        def save(self, rates, provenance, live_classes=None, *, now=None, fenced_on):
            self.save_entered.set()
            self.release.wait(timeout=5)
            self.snapshot = Snapshot(rates=dict(rates), provenance=dict(provenance),
                                     fetched_at=2.0, digest="new")
            return self.snapshot

    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=1.0, digest="d")
    store = _BlockingStore(stored)
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5")})
    # A long interval so the second `load()` below finds the table already fresh
    # rather than claiming a fetch of its own — this test is about the lock, not
    # about a second pass racing the first.
    source = LivePriceSource([feed], store=store,
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=3600)

    fetch_thread = threading.Thread(target=source.load)
    fetch_thread.start()
    assert store.save_entered.wait(timeout=5), "the blocked save never started"

    result: dict = {}

    def _concurrent_load() -> None:
        result["table"] = source.load()

    load_thread = threading.Thread(target=_concurrent_load)
    load_thread.start()
    load_thread.join(timeout=0.5)
    finished_quickly = not load_thread.is_alive()

    store.release.set()
    fetch_thread.join(timeout=5)
    load_thread.join(timeout=5)

    assert finished_quickly, (
        "a concurrent load() waited on the blocked save; the store's I/O must run "
        "outside the lock that guards the in-memory table"
    )
    assert result["table"]["opus"].input_per_mtok_microusd == 5_500_000, (
        "the concurrent reader must see the table this pass already produced"
    )


def test_a_floor_correction_for_a_leg_no_provider_publishes_reaches_the_charge(monkeypatch):
    """Q1: `_fallback_leg` always preferred the snapshot's stored number over the
    floor for any leg, so a cache leg that was itself floor-derived when the
    snapshot was written could never see a later correction to the bundled floor —
    the snapshot's old copy of the floor outranks the floor forever. `live_classes`
    already says which legs were genuinely live; the read side has to use it."""
    from mvp.pricing_feeds import composite as composite_mod

    stored = Snapshot(
        rates={"opus": Rate(5_500_000, 27_500_000, 9_000_000, 9_000_000)},
        fetched_at=1.0, digest="d",
        # Only input/output were ever live; the cache legs were floor-derived.
        live_classes={"opus": frozenset({"input", "output"})},
    )
    card = {
        (None, RateDimension("input", "geo")): Decimal("5.5"),
        (None, RateDimension("output", "geo")): Decimal("27.5"),
    }
    corrected_floor = {"opus": Rate(5_500_000, 27_500_000, 1_000_000, 1_200_000)}
    monkeypatch.setattr(composite_mod, "_floor_rates", lambda: corrected_floor)

    source = LivePriceSource([_Feed("f", {"anthropic.m": card})], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    rate = source.load()["opus"]
    assert rate.cache_read_per_mtok_microusd == 1_000_000, (
        "a leg no provider publishes must follow a floor correction, not the stale "
        "number the snapshot happened to store while it was floor-derived"
    )
    assert rate.cache_write_per_mtok_microusd == 1_200_000
    # The live legs are untouched by the floor correction.
    assert rate.input_per_mtok_microusd == 5_500_000
    assert rate.output_per_mtok_microusd == 27_500_000


def test_a_key_disagreement_on_a_cache_leg_is_named():
    """Q2: `key_price_disagreement` compared only input and output, so two models
    sharing a key that differ only on `cache_write` read as agreement — and the key
    is charged at the per-leg maximum with nothing telling the operator to split it.
    The vector has to be built from every field in `_FIELD_BY_CLASS`/`RATE_FIELDS`,
    not a hardcoded pair."""
    feed = _Feed("f", {
        "anthropic.a": _full_card("5", "25", "0.5", "6.25"),
        "anthropic.b": _full_card("5", "25", "0.5", "9.00"),   # only cache_write differs
    })
    source = LivePriceSource([feed], store=_Store(), registry=[
        _entry("us.anthropic.a", "opus"), _entry("us.anthropic.b", "opus"),
    ], interval_seconds=0)
    source.load()
    report = source.last_report()
    assert report is not None
    assert "opus" in report.key_price_disagreement, (
        "a difference confined to a leg other than input/output must still be "
        "reported as a key disagreement"
    )
    vectors = report.key_price_disagreement["opus"]
    assert all(len(v) == len(RATE_FIELDS) for v in vectors.values()), (
        "the disagreement vector must cover every rate leg, not only input and output"
    )
    idx = RATE_FIELDS.index("cache_write_per_mtok_microusd")
    # Registry spelling, not the price-API spelling the module resolved internally —
    # `_build`'s one-namespace rule: the same model appearing as two strings in two
    # lists is a report nobody can act on.
    assert {model: v[idx] for model, v in vectors.items()} == {
        "us.anthropic.a": 6_250_000, "us.anthropic.b": 9_000_000,
    }


def test_a_refresh_during_an_in_flight_load_does_not_start_a_third_pass():
    """Q7: `refresh()` does not check the in-flight claim, so a `refresh()` started
    while a request-path `load()` is still fetching opens M1's race locally — two
    passes building AWS clients concurrently on the default session, and a boolean
    claim that either one can clear while the other is still running. Real threads:
    a slow feed is asked to answer twice only if the bug is present."""
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    class _Slow:
        name = "slow"

        def fetch(self, request):
            calls["n"] += 1
            entered.set()
            release.wait(timeout=5)
            return FeedResult()

    source = LivePriceSource([_Slow()], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)

    load_thread = threading.Thread(target=source.load)
    load_thread.start()
    assert entered.wait(timeout=5), "the first, request-path pass never started"

    refresh_thread = threading.Thread(target=source.refresh)
    refresh_thread.start()
    # Give a buggy implementation a real window to start a second fetch before the
    # first is allowed to finish.
    _time.sleep(0.2)

    release.set()
    load_thread.join(timeout=5)
    refresh_thread.join(timeout=5)

    assert calls["n"] == 1, (
        "refresh() must join the in-flight pass rather than start a second one"
    )


def test_a_sibling_that_prices_the_key_does_not_erase_the_other_member_s_notice(monkeypatch):
    """Q8: `unpriced.pop` is keyed on whether the PRICING KEY folded at all, not on
    whether THIS model was priced — so a sibling on the same key that did get priced
    erases the only notice for a member whose failover region set could not be read.
    Rule 11 promises that model is named in the report; a shared key must not take
    that away."""
    from mvp.pricing_feeds import composite as composite_mod

    priced_entry = _entry("us.anthropic.a", "opus")
    unreadable_entry = _entry("us.anthropic.b", "opus")
    real_candidate_regions = composite_mod._candidate_regions

    def patched(entry):
        if entry is unreadable_entry:
            return None
        return real_candidate_regions(entry)

    monkeypatch.setattr(composite_mod, "_candidate_regions", patched)
    feed = _Feed("f", {
        "anthropic.a": _full_card("5", "25", "0.5", "6.25"),
        "anthropic.b": _full_card("15", "75", "1.5", "18.75"),
    })
    source = LivePriceSource([feed], store=_Store(), registry=[priced_entry, unreadable_entry],
                            interval_seconds=0)
    source.load()
    report = source.last_report()
    assert report is not None
    assert "us.anthropic.b" in report.unpriced, (
        "a sibling that priced the shared key must not erase this model's own notice "
        "that its region set was unreadable"
    )


def test_the_reported_budget_is_what_the_pass_actually_ran_with(monkeypatch):
    """Q15: a 300-second refresh that truncated at a 15-second request-path budget
    used to log "truncated at 15 s", because the warning always read the
    request-path default rather than the budget this pass actually ran with.
    `FetchReport.budget_seconds` has to carry the real number, and the log has to
    read it from there."""
    monkeypatch.setenv("STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS", "15")

    class _Slow:
        name = "slow"

        def fetch(self, request):
            _time.sleep(0.2)
            return FeedResult()

    source = LivePriceSource([_Slow(), _Slow()], store=_Store(),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    with capture_logs() as logs:
        report = source.refresh(budget_seconds=0.05)
    assert report.budget_seconds == 0.05, (
        "FetchReport must carry the budget this pass actually ran with, not the "
        "request-path default"
    )
    partial = [e for e in logs if e.get("event") == "price_feed_table_partial"]
    assert partial, logs
    assert partial[0].get("budget_seconds") == 0.05, (
        "the logged budget must be the one this pass used"
    )


def test_self_hosted_feed_prices_from_the_composite_s_own_registry(monkeypatch, tmp_path):
    """Q22: the default wiring builds `SelfHostedFeed()` with no registry, so it
    reads the process-global registry instead of the one `LivePriceSource` was
    constructed with — and matches on the invoked id while the request it receives
    carries the billing id (`price_model_id`). Either alone silently drops a
    self-hosted entry with a declared billing id to the floor. `AgreementFeed` and
    `PriceListFeed` are stubbed out here so the default wiring can run without
    reaching real AWS; only `SelfHostedFeed` is exercised for real."""
    import mvp.pricing_feeds.agreement as agreement_mod
    import mvp.pricing_feeds.price_list as price_list_mod

    class _NoOpFeed:
        name = "noop"

        def fetch(self, request):
            return FeedResult()

    monkeypatch.setattr(agreement_mod, "AgreementFeed", _NoOpFeed)
    monkeypatch.setattr(price_list_mod, "PriceListFeed", _NoOpFeed)
    monkeypatch.setattr("mvp.models.registry_entries", lambda: ())

    doc = tmp_path / "selfhosted.json"
    doc.write_text(json.dumps({
        "schema_version": 1,
        "rates": {"pool-a": {"input_per_mtok_usd": "0.20", "output_per_mtok_usd": "0.20"}},
    }))
    monkeypatch.setenv("STRATOCLAVE_SELFHOSTED_RATES_PATH", str(doc))

    entry = ModelEntry(provider="nvidia", bedrock_model_id="local.llama",
                      bedrock_region="us-east-1", aliases=("local.llama",),
                      wire_protocol="responses", pricing_key="local",
                      served_by="vllm", endpoint_key="pool-a",
                      price_model_id="local.llama-billing")
    # feeds=None so the source's *own* default wiring is exercised, since the
    # defect is in that wiring rather than in SelfHostedFeed's own defaults.
    source = LivePriceSource(None, store=_Store(), registry=[entry], interval_seconds=0)
    table = source.load()
    assert table.get("local") == Rate(200_000, 200_000, 0, 0), (
        "SelfHostedFeed must price from the registry the composite was built with, "
        "matched on the id the request carries (the declared billing id), not the "
        "process-global registry matched on the invoked id"
    )


def test_a_cold_task_with_a_fresh_snapshot_skips_the_first_fetch():
    """M13: `_due()` returns `True` whenever nothing has been fetched yet in THIS
    process, so a cold task that just loaded a full, recent snapshot still pays for
    a synchronous fetch before its first answer — against the documented reason the
    snapshot is filled at deploy time: so no task ever races the feeds."""
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5")})
    now = 1_000_000.0
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=now - 10.0, digest="d")
    source = LivePriceSource([feed], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=3600, clock=lambda: now)
    table = source.load()
    assert feed.calls == 0, "a snapshot younger than the interval must not be refetched"
    assert table["opus"] == Rate(1, 2, 3, 4)


def test_a_cold_task_with_a_stale_snapshot_still_fetches():
    """M13's counterpart: the fix must not overcorrect into never fetching cold. A
    snapshot older than the interval still has to be refreshed on the first
    `load()`."""
    feed = _Feed("f", {"anthropic.m": _full_card("5.5", "27.5")})
    now = 1_000_000.0
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=now - 7200.0, digest="d")
    source = LivePriceSource([feed], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=3600, clock=lambda: now)
    source.load()
    assert feed.calls == 1, "a snapshot older than the interval must still be refreshed"


def test_a_pass_that_prices_nothing_warns_with_the_stored_count_and_age():
    """M7: a pass that produces nothing is quieter than a pass that produces less —
    charging keeps going at an ageing table with no repeating signal unless this
    warning fires and names what it is ageing. Only the warning and the reported
    age are asserted here; the `last_seen_at` half of M7 belongs to Q9, tracked by
    another author."""
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4), "sonnet": Rate(5, 6, 7, 8)},
                      fetched_at=_time.time() - 100.0, digest="d")
    source = LivePriceSource([_Feed("f", {})], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0)
    with capture_logs() as logs:
        source.load()
    warnings = [e for e in logs if e.get("event") == "price_feed_fetch_empty"]
    assert warnings, logs
    assert warnings[0].get("log_level") == "warning"
    assert warnings[0].get("stored_keys") == 2
    age = warnings[0].get("snapshot_age_seconds")
    assert age is not None and 90 <= age <= 130, (
        f"snapshot_age_seconds should read roughly 100s old, got {age!r}"
    )


def test_the_empty_pass_warning_repeats_across_passes():
    """M7: a signal that fires once and goes quiet again is exactly as useless as no
    signal at all, since the table keeps ageing after the first warning."""
    now = [1_000_100.0]
    stored = Snapshot(rates={"opus": Rate(1, 2, 3, 4)}, fetched_at=1_000_000.0, digest="d")
    source = LivePriceSource([_Feed("f", {})], store=_Store(stored),
                            registry=[_entry("us.anthropic.m", "opus")],
                            interval_seconds=0, clock=lambda: now[0])
    with capture_logs() as first_logs:
        source.load()
    now[0] += 3600
    with capture_logs() as second_logs:
        source.load()
    for logs in (first_logs, second_logs):
        warnings = [e for e in logs if e.get("event") == "price_feed_fetch_empty"]
        assert warnings, logs
        assert warnings[0].get("stored_keys") == 1
        assert warnings[0].get("snapshot_age_seconds") is not None
