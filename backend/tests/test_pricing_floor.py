"""The bundled floor holds MEASURED list prices, and this is where they are pinned.

Everywhere else in the suite the expected charge is derived from
`pricing.baseline_rates()`, so that a price change does not fail thirty unrelated
tests. That leaves exactly one thing unguarded — whether the floor itself is right —
and this file is it. Q20: what follows this docstring, up to
`test_no_floor_leg_undercuts_the_live_published_price`, is a DIFF gate between two
copies living in this same file — `MEASURED` here and `defaults/pricing.json` — so a
change to either one alone fails immediately, with the provenance of the new number
expected in the diff. Neither copy moves when the provider's own price does, so that
diff gate is not, by itself, a check against what AWS currently publishes.
`test_no_floor_leg_undercuts_the_live_published_price`, gated behind
`STRATOCLAVE_LIVE_PRICE_TESTS`, is the one test in this file that reads the provider
directly and is the only thing here a real price move can fail.

Every figure below was read from the provider's own APIs on 2026-08-31:

- `bedrock:ListFoundationModelAgreementOffers` for the Marketplace-metered families
  (all Anthropic generations, OpenAI GPT-5.x). This is the only source that carries
  the current Claude prices; the Price List API's `AmazonBedrock` offer stops at
  Claude 3 Sonnet.
- The Price List API (`AmazonBedrock`, and `AmazonBedrockFoundationModels` for the
  legacy Anthropic rows) for the families AWS bills directly.

They are the IN-REGION (geo / "Regional CRIS") standard-tier rates, because every
registry entry addresses its model through a `us.` inference profile and that is
what such a request is billed at. The `global` rate is ~10% lower and is not the
floor: the floor's job is to be safe when a feed is unavailable.
"""
from __future__ import annotations

import os

import pytest

from mvp.models import registry_entries
from mvp.pricing import baseline_rates
from tests.live_aws import real_session

_LIVE_FLAG = "STRATOCLAVE_LIVE_PRICE_TESTS"

# pricing key -> (input, output, cache_read, cache_write) in micro-USD per MTok.
MEASURED = {
    "opus": (5_500_000, 27_500_000, 550_000, 6_875_000),
    "opus-legacy": (15_000_000, 75_000_000, 1_500_000, 18_750_000),
    "fable": (11_000_000, 55_000_000, 1_100_000, 13_750_000),
    "sonnet": (3_300_000, 16_500_000, 330_000, 4_125_000),
    "sonnet-5": (2_200_000, 11_000_000, 220_000, 2_750_000),
    "sonnet-3": (3_000_000, 15_000_000, 300_000, 3_750_000),
    "haiku": (1_100_000, 5_500_000, 110_000, 1_375_000),
    "haiku-3-5": (800_000, 4_000_000, 80_000, 1_000_000),
    "haiku-3": (250_000, 1_250_000, 250_000, 312_500),
    "gpt-5.6-sol": (5_500_000, 33_000_000, 550_000, 6_875_000),
    "gpt-5.6-terra": (2_200_000, 13_200_000, 275_000, 3_437_500),
    "grok": (2_200_000, 6_600_000, 550_000, 2_750_000),
    "gemma": (140_000, 400_000, 140_000, 175_000),
    "nemotron": (150_000, 650_000, 150_000, 187_500),
    "qwen": (140_000, 1_200_000, 140_000, 175_000),
}

# Keys whose cache legs are an UPPER BOUND rather than a published price, because
# the provider publishes none. Listed here so the next reader can tell an assumption
# from a measurement without opening the JSON.
CACHE_LEGS_ASSUMED = {"haiku-3", "gemma", "nemotron", "qwen", "grok"}

# `default` is what a pricing key that exists nowhere is charged, which in practice means
# a typo. The document says an unpriced model over-charges rather than under-charges, so
# `default` has to dominate every provider row leg by leg — a fallback cheaper than a real
# tier under-charges exactly the models that tier covers. There is no exception list: an
# exception here is the rule not holding.


@pytest.mark.parametrize("key", sorted(MEASURED))
def test_floor_matches_the_measured_list_price(key):
    rate = baseline_rates()[key]
    assert (rate.input_per_mtok_microusd, rate.output_per_mtok_microusd,
            rate.cache_read_per_mtok_microusd, rate.cache_write_per_mtok_microusd) \
        == MEASURED[key], (
        f"{key}: the floor no longer matches the measured list price. If AWS moved "
        f"the price, update MEASURED here and defaults/pricing.json together, and "
        f"say in the row's `notes` when it was measured."
    )


def test_every_registry_key_has_a_floor_row():
    """A key that exists only in a live feed is not enough: the floor is the layer
    guaranteed to load, so such a model would be charged at `default` the moment the
    feed is unavailable."""
    floor = baseline_rates()
    missing = sorted({e.pricing_key for e in registry_entries()} - set(floor))
    assert not missing, f"registry pricing keys with no floor row: {missing}"


def test_no_leg_is_free_except_self_hosted_cache():
    """A zero leg is a discount, not a price. The one legitimate zero is vLLM's cache
    pair: vLLM reports no Bedrock-style cache split, so those tokens are already
    counted as input."""
    for key, rate in baseline_rates().items():
        assert rate.input_per_mtok_microusd > 0, key
        assert rate.output_per_mtok_microusd > 0, key
        if key == "vllm":
            assert rate.cache_read_per_mtok_microusd == 0
            assert rate.cache_write_per_mtok_microusd == 0
            continue
        assert rate.cache_read_per_mtok_microusd > 0, key
        assert rate.cache_write_per_mtok_microusd > 0, key


def test_default_dominates_every_bundled_provider_rate_leg():
    """A pricing key that exists nowhere is charged at `default`, and the document promises
    that such a model over-charges. That promise is only true if `default` is at least as
    dear as every provider row, on every leg — otherwise a typo in the registry
    under-charges by the difference, silently, for as long as it goes unnoticed."""
    from mvp.rates import RATE_FIELDS

    floor = baseline_rates()
    default = floor["default"]
    for key, rate in floor.items():
        if key in {"default", "vllm"}:      # vllm is operator cost recovery, not a list price
            continue
        for leg in RATE_FIELDS:
            assert getattr(rate, leg) <= getattr(default, leg), (
                f"{key}.{leg} out-prices `default`, so a pricing key that exists nowhere "
                f"would UNDER-charge. Raise `default` to at least the dearest row."
            )


def test_assumed_cache_legs_are_declared_in_the_document():
    """A number nobody published has to say so where it lives, not only in a test."""
    from mvp.price_sources import pricing_path
    import json

    doc = json.load(open(pricing_path(), encoding="utf-8"))
    for key in CACHE_LEGS_ASSUMED:
        notes = (doc["rates"][key].get("notes") or "").lower()
        assert "upper bound" in notes, (
            f"{key}'s cache legs are an assumption; say so in its `notes`"
        )


def test_a_pricing_key_holds_one_price_point():
    """Two models on one key are charged at the dearest of them, so a key spanning
    two price points over-charges the cheaper model on every request.

    This is not hypothetical: `opus` covered Claude Opus 4.1 ($15/$75) and Opus 5
    ($5.50/$27.50) until the live feed measured them apart, which would have charged
    every Opus 5 request at nearly three times its rate. The registry now splits
    them, and this test is what keeps a future model from being filed under a key it
    does not belong to.
    """
    by_key: dict[str, set[str]] = {}
    for entry in registry_entries():
        by_key.setdefault(entry.pricing_key, set()).add(entry.bedrock_model_id)
    # The check is on the DECLARED grouping: models sharing a key must be documented
    # as sharing a price. `notes` on the key's row (or the entry) is where that is
    # said, and MEASURED above is the number they share.
    for key, models in sorted(by_key.items()):
        assert key in MEASURED or key in {"vllm", "default"}, (
            f"pricing key {key!r} is used by {sorted(models)} but has no measured "
            f"price pinned here"
        )


def test_assumed_cache_leg_set_is_derived_from_the_document_not_hardcoded_twice():
    """M9. `price-feeds.md:43` and CONTRACTS.md's C2.8 say every floor row is a
    measured in-region list price; the document's own `notes` say otherwise for
    five of them (`haiku-3`, `grok`, `gemma`, `nemotron`, `qwen` all say their cache
    legs are an UPPER BOUND, not a published number). `CACHE_LEGS_ASSUMED` above
    already carries that distinction — `test_assumed_cache_legs_are_declared_in_the_
    document` checks it forward, that every key IN the set says so in its notes — but
    that check is one-directional: a sixth row could start saying 'upper bound' in its
    notes without anyone adding it to `CACHE_LEGS_ASSUMED`, and nothing here would
    notice. This derives the assumed set FROM the document's own notes independently
    of the hand-maintained constant and requires the two to agree exactly, so a
    document row that becomes an assumption without updating this file fails here
    rather than only being true by one copy trusting the other."""
    from mvp.price_sources import pricing_path
    import json

    doc = json.load(open(pricing_path(), encoding="utf-8"))
    derived = {
        key for key, row in doc["rates"].items()
        if "upper bound" in (row.get("notes") or "").lower()
    }
    assert derived == CACHE_LEGS_ASSUMED, (
        f"the document's own notes say the assumed-cache-leg set is "
        f"{sorted(derived)}; CACHE_LEGS_ASSUMED here says {sorted(CACHE_LEGS_ASSUMED)}. "
        f"A row whose cache legs are an assumption has to say 'upper bound' in its "
        f"notes AND be listed in CACHE_LEGS_ASSUMED, or the claim price-feeds.md:43 "
        f"and C2.8 make is not checkable from the data it describes."
    )


def _price_feeds_doc_path():
    import pathlib

    # backend/tests/test_pricing_floor.py -> parents[1] = backend, .parent = repo root.
    root = pathlib.Path(__file__).resolve().parents[1].parent
    return root / "docs" / "design" / "price-feeds.md"


def _non_measured_floor_rows() -> set[str]:
    """The keys `price-feeds.md:43` and C2.8's unqualified 'every row' /
    'the bundled floor' claims are false about, derived from the bundled
    document's own `notes` rather than hand-copied here a second time: the
    cache-leg-assumed set (whatever currently says 'upper bound') plus the three
    rows that are not measured at all, whatever their leg. A new assumed row, or
    a new not-measured row, changes this set on its own -- which is the point:
    the two sentences this feeds have to become STALE the moment the document's
    own data disagrees with them, not stay green because nobody looked twice."""
    from mvp.price_sources import pricing_path
    import json

    doc = json.load(open(pricing_path(), encoding="utf-8"))
    assumed_cache = {
        key for key, row in doc["rates"].items()
        if "upper bound" in (row.get("notes") or "").lower()
    }
    return assumed_cache | {"gpt-5", "default", "vllm"}


def test_price_feeds_doc_states_measured_per_leg_not_every_row():
    """M9, half one. `docs/design/price-feeds.md:43` says: "Every row in
    `defaults/pricing.json` is a measured in-region list price" -- unqualified
    over every ROW. The document's own `notes` contradict it for the set derived
    in `_non_measured_floor_rows` above: five rows whose cache legs are a stated
    upper bound rather than a measurement, `gpt-5` (the dearest measured GPT-5.6
    tier, not itself measured), `default` (synthetic) and `vllm` (an operator
    figure). The repair the contract asks for is per-leg precision -- a
    provider-published leg is measured, an unpublished leg is a stated
    conservative upper bound, and `default`/`vllm` are neither -- not weakening
    the sentence until it says nothing. This fails while the unqualified 'every
    row ... is a measured ... list price' form is still in the document, and it
    derives the exception set from the document's own data so a new assumed or
    synthetic row makes this sentence stale on its own, rather than staying
    green by nobody having looked."""
    import re

    from mvp.price_sources import pricing_path
    import json

    doc = json.load(open(pricing_path(), encoding="utf-8"))
    non_measured = _non_measured_floor_rows()
    assert non_measured <= set(doc["rates"]), (
        f"fixture assumption broken: {sorted(non_measured - set(doc['rates']))} "
        f"no longer exist in defaults/pricing.json"
    )

    doc_path = _price_feeds_doc_path()
    text = doc_path.read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", text)
    unqualified = re.search(
        r"[Ee]very row in .*? is a measured in-region list price", normalized
    )
    assert unqualified is None, (
        f"docs/design/price-feeds.md still says {unqualified.group()!r} -- an "
        f"unqualified claim over EVERY row, contradicted by the document's own "
        f"notes for {sorted(non_measured)} (cache legs stated as an upper bound "
        f"for the assumed set, or not measured at all for gpt-5/default/vllm). "
        f"Restate per leg: a provider-published leg is measured, an unpublished "
        f"leg is a stated conservative upper bound, default/vllm are neither."
    )


def test_contracts_c2_8_states_measured_per_leg_not_the_whole_floor():
    """M9, half two. `docs/design/CONTRACTS.md`'s C2.8 says: "The bundled floor
    is a measured list price with its provenance recorded, not a placeholder" --
    the same unqualified claim as price-feeds.md:43, over the WHOLE floor this
    time rather than 'every row', in the file a dispute is answered from. Same
    exception set, same repair, same requirement that the exception set be
    derived from the document's own notes rather than hand-copied a second time
    in CONTRACTS.md's row."""
    import re

    non_measured = _non_measured_floor_rows()

    contracts_path = _price_feeds_doc_path().parent / "CONTRACTS.md"
    text = contracts_path.read_text(encoding="utf-8")
    rows = [line for line in text.split("\n") if line.startswith("| **C2.8**")]
    assert rows, "C2.8 not found in docs/design/CONTRACTS.md"
    row = rows[0]

    unqualified = re.search(r"bundled floor is a measured list price", row)
    assert unqualified is None, (
        f"CONTRACTS.md's C2.8 still says {unqualified.group()!r} "
        f"-- unqualified over the WHOLE bundled floor, contradicted by "
        f"defaults/pricing.json's own notes for {sorted(non_measured)}. Restate "
        f"per leg, the same repair as price-feeds.md:43: a provider-published leg "
        f"is measured, an unpublished leg is a stated conservative upper bound, "
        f"and default/vllm are neither."
    )


@pytest.mark.live
def test_no_floor_leg_undercuts_the_live_published_price():
    """Q20. `test_floor_matches_the_measured_list_price` above compares
    `baseline_rates()` (which reads `defaults/pricing.json`) against `MEASURED`, a
    hand-written copy of the same numbers in this same file. That is a gate on the
    two copies agreeing with EACH OTHER — it fails when someone edits one without the
    other — and this module's docstring used to describe it as a gate on the
    provider, which it is not: neither copy moves when AWS raises a price, so a real
    price increase the floor has gone stale against would pass silently forever.

    This is the test that reads the provider directly: `bedrock:
    ListFoundationModelAgreementOffers` and `pricing:GetProducts`, for the same keys
    the floor prices, and asserts no floor leg is CHEAPER than what is published
    right now. A floor leg below the live number under-charges every request that
    falls to it while a feed is unavailable — the exact failure the floor exists to
    prevent.

    Skipped by default and gated behind `STRATOCLAVE_LIVE_PRICE_TESTS` plus real
    credentials, the same convention `tests/test_pricing_feeds_live_apis.py` and
    `tests/test_pricing_feeds_prefixes.py` use — this is a phase 5 check, not a
    per-commit one.
    """
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"set {_LIVE_FLAG}=1 (with AWS credentials) to check the floor "
                    f"against the live price APIs")
    boto3 = pytest.importorskip("boto3")
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")

    from mvp.pricing_feeds.agreement import AgreementFeed
    from mvp.pricing_feeds.composite import LivePriceSource
    from mvp.pricing_feeds.price_list import PriceListFeed
    from mvp.rates import RATE_FIELDS

    class _NoStore:
        """Reads and writes nothing: a live check must not touch the snapshot the
        running deployment charges from."""

        def load(self):
            return None

        def save(self, *args, **kwargs):
            return None

    region = os.getenv("STRATOCLAVE_REGION") or "us-east-1"
    feeds = (
        AgreementFeed(session.client("bedrock", region_name=region)),
        PriceListFeed(session.client("pricing", region_name="us-east-1")),
    )
    source = LivePriceSource(feeds, store=_NoStore(), interval_seconds=0)
    report = source.refresh()
    if not report.rates and report.feed_errors:
        pytest.skip(f"price APIs unreachable with these credentials: {report.feed_errors}")

    floor = baseline_rates()
    too_low = []
    for key, live_rate in report.rates.items():
        floor_rate = floor.get(key)
        if floor_rate is None:
            continue
        for leg in RATE_FIELDS:
            floor_value = getattr(floor_rate, leg)
            live_value = getattr(live_rate, leg)
            if floor_value < live_value:
                too_low.append(f"{key}.{leg}: floor {floor_value} < live {live_value}")
    assert not too_low, (
        "the bundled floor is CHEAPER than what the provider currently publishes, "
        "which under-charges every request that falls to it while a feed is down:\n"
        + "\n".join(too_low)
    )
