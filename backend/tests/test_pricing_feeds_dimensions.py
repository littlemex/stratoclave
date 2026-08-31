"""The rate-name parsers, against the real names the providers publish.

The fixtures under `tests/fixtures/pricing_feeds/` are trimmed copies of real API
responses captured on 2026-08-31 (the signed `offerToken` is stripped). They exist
because the thing most likely to break this subsystem is a name changing shape, and
a hand-written string is not evidence of what AWS emits.

Three grammars are live at once, and every one of them is exercised here:
CamelCase (`USW2_InputTokenCount_Global`), snake_case with a region prefix
(`USW2_input_tokens_global_standard`), and snake_case without one
(`input_tokens_standard`, the OpenAI shape).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from mvp.pricing_feeds.dimensions import (
    BILLING_PREFIXES,
    EXCLUDED,
    REGION_TO_PREFIX,
    RateDimension,
    base_model_id,
    parse_agreement_dimension,
    parse_price_list_usagetype,
    per_mtok,
    scope_for_model_id,
    select,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "pricing_feeds"


def _card(fixture: str) -> dict:
    doc = json.loads((_FIXTURES / fixture).read_text())
    rows = doc["offers"][0]["termDetails"]["usageBasedPricingTerm"]["rateCard"]
    return {r["dimension"]: r["price"] for r in rows}


# --- the three agreement grammars -------------------------------------------
@pytest.mark.parametrize("name,expected", [
    # CamelCase, generation 1 (Claude Sonnet 4.6, Haiku 4.5 and older).
    ("USW2_InputTokenCount_Global", ("us-west-2", RateDimension("input", "global"))),
    ("USW2_OutputTokenCount_Geo", ("us-west-2", RateDimension("output", "geo"))),
    ("USE1_CacheReadInputTokenCount_Global", ("us-east-1", RateDimension("cache_read", "global"))),
    ("USE1_CacheWriteInputTokenCount_Geo", ("us-east-1", RateDimension("cache_write", "geo"))),
    ("USW2_InputTokenCount_LCtx_Global", ("us-west-2", RateDimension("input", "global", True))),
    ("USW2_InputTokenCount_Global_Batch",
     ("us-west-2", RateDimension("input", "global", False, "batch"))),
    # An unqualified name means the in-region price (legacy Opus 4.1, Claude 3).
    ("USW2_InputTokenCount", ("us-west-2", RateDimension("input", "geo"))),
    # snake_case with a region, generation 2 (Opus 4.7+, Sonnet 5, Fable 5).
    ("USW2_input_tokens_global_standard", ("us-west-2", RateDimension("input", "global"))),
    ("USW2_output_tokens_standard", ("us-west-2", RateDimension("output", "geo"))),
    ("USW2_cache_read_tokens_global_standard", ("us-west-2", RateDimension("cache_read", "global"))),
    ("USW2_cache_write_tokens_standard", ("us-west-2", RateDimension("cache_write", "geo"))),
    ("USW2_input_tokens_global_batch",
     ("us-west-2", RateDimension("input", "global", False, "batch"))),
    # snake_case without a region, generation 3 (the OpenAI cards).
    ("input_tokens_standard", (None, RateDimension("input", "geo"))),
    ("output_tokens_global_standard", (None, RateDimension("output", "global"))),
    ("cached_input_tokens_batch", (None, RateDimension("cache_read", "geo", False, "batch"))),
    ("cache_read_tokens_long_ctx_global_standard",
     (None, RateDimension("cache_read", "global", True))),
    ("input_tokens_long_ctx_flex", (None, RateDimension("input", "geo", True, "flex"))),
])
def test_agreement_dimensions_parse(name, expected):
    assert parse_agreement_dimension(name) == expected


@pytest.mark.parametrize("name", [
    # Priced per TPM-hour or per model-unit-hour, not per token.
    "USW2_Reserved_1Month_InputTPM_Geo",
    "USW2_ProvisionedThroughput_6MonthsCommit_ModelUnits_Usage",
    "USE1_ProvisionedThroughput_NoCommit_ModelUnits_Usage",
    # A cache write with a non-default TTL is a different product (1h costs double the
    # 5-minute default) and the rate table has one cache-write leg.
    "USW2_CacheWrite1hInputTokenCount_Global",
    "USW2_cache_write_tokens_1h_standard",
    "cache_write_tokens_30m_flex",
])
def test_recognised_names_this_gateway_does_not_charge_are_excluded_not_unparsed(name):
    """EXCLUDED, not None. Every Claude card carries a 1h cache write and every model
    carries provisioned-throughput rows, so counting them as unparsed would leave that
    counter permanently nonzero — and `unparsed` is the only signal that a TOKEN price
    changed shape."""
    assert parse_agreement_dimension(name) is EXCLUDED


@pytest.mark.parametrize("name", [
    # Shapes this build does not know. Refused rather than guessed, and counted.
    "USW2_InputTokenCount_Quantum",
    "input_tokens_something_new",
    "",
])
def test_unknown_shapes_are_none_so_they_are_counted(name):
    assert parse_agreement_dimension(name) is None


def test_a_renamed_grammar_does_not_raise():
    """The contract that keeps a provider rename from taking charging down: parsing
    returns None, it never throws, whatever it is handed."""
    for name in (None, 123, "___", "_" * 400, "USW2_", "🙂"):
        assert parse_agreement_dimension(name) is None  # type: ignore[arg-type]


# --- the Price List grammar --------------------------------------------------
@pytest.mark.parametrize("usagetype,model_id,expected", [
    ("USE1-xai.grok-4.6-mantle-input-tokens-standard", "xai.grok-4.6",
     ("us-east-1", RateDimension("input", "geo"))),
    ("USE1-xai.grok-4.6-mantle-input-tokens-global-standard", "xai.grok-4.6",
     ("us-east-1", RateDimension("input", "global"))),
    ("USE1-xai.grok-4.6-mantle-cache-read-tokens-standard", "xai.grok-4.6",
     ("us-east-1", RateDimension("cache_read", "geo"))),
    ("USW2-nvidia.nemotron-super-3-120b-mantle-output-tokens-standard",
     "nvidia.nemotron-super-3-120b", ("us-west-2", RateDimension("output", "geo"))),
    # The older form, with no `mantle` marker.
    ("USE1-nvidia.nemotron-super-3-120b-input-tokens-flex",
     "nvidia.nemotron-super-3-120b", ("us-east-1", RateDimension("input", "geo", False, "flex"))),
    # The billed id carries a segment the registry id does not, so the registry
    # DECLARES the billed id (`price_model_id`) and it is passed here.
    ("USE1-qwen.qwen3-next-80b-a3b-instruct-mantle-input-tokens-standard",
     "qwen.qwen3-next-80b-a3b-instruct", ("us-east-1", RateDimension("input", "geo"))),
])
def test_price_list_usagetypes_parse(usagetype, model_id, expected):
    assert parse_price_list_usagetype(usagetype, model_id) == expected


def test_price_list_usagetype_requires_the_right_model():
    """Anchoring on the model id is what stops one model's rows being charged to
    another's key — matching by "contains" would let a longer id's rows land on a
    shorter one."""
    assert parse_price_list_usagetype(
        "USE1-xai.grok-4.6-mantle-input-tokens-standard", "xai.grok-4.3") is None
    assert parse_price_list_usagetype("ZZZ9-xai.grok-4.6-mantle-input-tokens-standard",
                                     "xai.grok-4.6") is None


def test_variant_rows_never_attach_to_a_shorter_registered_id():
    """The dangerous near-miss: `xai.grok-4` is a prefix of `xai.grok-4.6`, and the
    two are different models at different prices. Absorbing the extra segment would
    charge every Grok 4 request at Grok 4.6's rate — and it would only show up as a
    bigger invoice, because the folded card takes the dearer number by design.

    A model whose billed id really does carry an extra segment declares it in the
    registry (`price_model_id`); nothing is guessed here.
    """
    assert parse_price_list_usagetype(
        "USE1-xai.grok-4.6-mantle-input-tokens-global-standard", "xai.grok-4") is None
    assert parse_price_list_usagetype(
        "USE1-qwen.qwen3-next-80b-a3b-instruct-mantle-input-tokens-standard",
        "qwen.qwen3-next-80b-a3b") is None


def test_the_default_cache_ttl_is_the_base_product():
    """5 minutes is Bedrock's default prompt-cache TTL, so a card that spells it out is
    publishing the base cache-write rate under a longer name. Dropping it would leave
    the leg on the floor while the provider was publishing it all along; a 1h rate is a
    genuinely different product at double the price and stays excluded."""
    assert parse_agreement_dimension("USW2_cache_write_tokens_5m_standard") == (
        "us-west-2", RateDimension("cache_write", "geo"))
    assert parse_agreement_dimension("USW2_CacheWrite5mInputTokenCount_Global") == (
        "us-west-2", RateDimension("cache_write", "global"))
    assert parse_agreement_dimension("USW2_cache_write_tokens_1h_standard") is EXCLUDED
    assert parse_agreement_dimension("cache_write_tokens_30m_flex") is EXCLUDED


def test_an_unrecognised_profile_prefix_is_named_rather_than_guessed_at():
    """AWS keeps adding cross-region prefixes. One this build has not seen is NOT stripped
    on a guess — that rule mangles a bare id like `xai.grok-4.6`, whose second dot belongs
    to a version number — and it is not ignored either, because the price APIs reject a
    prefixed id and the model would drop off the feed silently. It is named, so the
    registry entry can settle it with `price_model_id`."""
    from mvp.pricing_feeds.dimensions import unknown_profile_prefix

    assert unknown_profile_prefix("mx.anthropic.claude-opus-5") == "mx"
    assert base_model_id("mx.anthropic.claude-opus-5") == "mx.anthropic.claude-opus-5"
    # Scope unknown, so the selector takes the dearer of the two rather than the cheaper.
    assert scope_for_model_id("mx.anthropic.claude-opus-5") is None

    # A bare provider id with a version dot is NOT a prefixed id, and a known prefix is
    # not "unknown".
    assert unknown_profile_prefix("xai.grok-4.6") is None
    assert base_model_id("xai.grok-4.6") == "xai.grok-4.6"
    assert unknown_profile_prefix("us.anthropic.claude-opus-5") is None


# --- units ------------------------------------------------------------------
def test_units_are_normalised_and_an_unknown_one_is_refused():
    """The same kind of number arrives per 1K tokens from the Price List and per 1M
    from an agreement card. Guessing wrong is a 1000-fold pricing error, so an unknown
    unit yields nothing at all."""
    assert per_mtok("0.0022", "1K tokens") == Decimal("2.2")
    assert per_mtok("5.5", "1M tokens") == Decimal("5.5")
    assert per_mtok("5.5", "Units") == Decimal("5.5")
    assert per_mtok("5.5", "per 1000 requests") is None
    assert per_mtok("not-a-number", "1M tokens") is None
    assert per_mtok("-1", "1M tokens") is None


# --- model id handling ------------------------------------------------------
def test_profile_prefix_decides_scope_and_is_stripped_for_the_query():
    """Both price APIs key on the bare id — `list-foundation-model-agreement-offers`
    rejects `us.anthropic.claude-opus-5` outright — while the prefix is what says which
    published scope the traffic is billed at."""
    assert base_model_id("us.anthropic.claude-opus-5") == "anthropic.claude-opus-5"
    assert base_model_id("global.anthropic.claude-haiku-4-5") == "anthropic.claude-haiku-4-5"
    assert base_model_id("nvidia.nemotron-super-3-120b") == "nvidia.nemotron-super-3-120b"
    assert scope_for_model_id("us.anthropic.claude-opus-5") == "geo"
    assert scope_for_model_id("global.anthropic.claude-opus-5") == "global"
    assert scope_for_model_id("anthropic.claude-opus-5") == "geo"
    assert scope_for_model_id("some-bare-name") is None


def test_billing_prefix_table_is_a_bijection():
    assert len(REGION_TO_PREFIX) == len(BILLING_PREFIXES)
    assert BILLING_PREFIXES["USE1"] == "us-east-1"
    assert BILLING_PREFIXES["APN1"] == "ap-northeast-1"
    assert BILLING_PREFIXES["EU"] == "eu-west-1"


# --- selection --------------------------------------------------------------
def test_selection_prices_opus_5_in_region_from_the_real_card():
    """The number this produces is the one the ledger charges, so it is checked
    against the figure the Price List publishes independently for the same model:
    $5.50 per MTok input in-region, $5.00 global."""
    card = {}
    for name, price in _card("agreement_opus5.json").items():
        parsed = parse_agreement_dimension(name)
        if parsed is None or parsed is EXCLUDED:
            continue
        card[parsed] = per_mtok(price, "Units")
    geo = select(card, regions=["us-west-2"], scope="geo")
    assert geo is not None and geo.rates["input"] == Decimal("5.5")
    assert geo.rates["output"] == Decimal("27.5")
    assert geo.absent == frozenset()
    glob = select(card, regions=["us-west-2"], scope="global")
    assert glob is not None and glob.rates["input"] == Decimal("5")


def test_selection_takes_the_dearest_candidate_region():
    """A Converse request can fail over mid-flight, so a reservation priced at the
    cheaper region would settle above what it admitted."""
    card = {
        ("us-east-1", RateDimension("input", "geo")): Decimal("3"),
        ("us-west-2", RateDimension("input", "geo")): Decimal("4"),
        ("us-east-1", RateDimension("output", "geo")): Decimal("10"),
        ("us-west-2", RateDimension("output", "geo")): Decimal("10"),
        ("us-east-1", RateDimension("cache_read", "geo")): Decimal("1"),
        ("us-east-1", RateDimension("cache_write", "geo")): Decimal("2"),
    }
    chosen = select(card, regions=["us-east-1", "us-west-2"], scope="geo")
    assert chosen is not None and chosen.rates["input"] == Decimal("4")


def test_a_class_the_provider_never_prices_is_absent_not_zero():
    """Nemotron and Qwen publish input and output only. Their cache legs must come
    back as absent so the caller fills them from the layer below — publishing zero
    would make cached tokens free."""
    card = {
        (None, RateDimension("input", "geo")): Decimal("0.15"),
        (None, RateDimension("output", "geo")): Decimal("0.65"),
    }
    chosen = select(card, regions=["us-east-1"], scope="geo")
    assert chosen is not None
    assert chosen.rates == {"input": Decimal("0.15"), "output": Decimal("0.65")}
    assert chosen.absent == frozenset({"cache_read", "cache_write"})


def test_a_leg_priced_only_elsewhere_is_widened_at_the_dearer_rate_and_reported():
    """Different from absent: the provider DOES price it, just not for the region or
    scope this request would use. Refusing the model sends every leg to the floor,
    which is the larger error; widening keeps it on live prices in the over-charging
    direction and says so, because a widened leg means the routing assumption and the
    price list disagree.

    This is not hypothetical: Claude Opus 5 publishes its cache legs in-region only, so
    a deployment addressing it through a `global.` profile has no in-scope cache rate.
    """
    card = {
        (None, RateDimension("input", "global")): Decimal("5"),
        (None, RateDimension("output", "global")): Decimal("25"),
        (None, RateDimension("cache_read", "geo")): Decimal("0.55"),
        (None, RateDimension("cache_write", "geo")): Decimal("6.875"),
    }
    chosen = select(card, regions=[], scope="global")
    assert chosen is not None
    assert chosen.rates["cache_read"] == Decimal("0.55")
    assert chosen.widened == frozenset({"cache_read", "cache_write"})
    assert chosen.absent == frozenset()


def test_a_model_with_no_input_or_output_at_all_is_still_refused():
    """The one refusal that stays: without input and output there is nothing to charge
    a request with, and inventing either from a long-context or batch number would
    price ordinary traffic at another product's rate."""
    card = {
        (None, RateDimension("input", "geo", True)): Decimal("11"),
        (None, RateDimension("cache_read", "geo")): Decimal("0.55"),
    }
    assert select(card, regions=["us-east-1"], scope="geo") is None


def test_long_context_and_non_standard_tiers_never_answer():
    card = {
        (None, RateDimension("input", "geo", True)): Decimal("11"),
        (None, RateDimension("input", "geo", False, "flex")): Decimal("2"),
        (None, RateDimension("output", "geo")): Decimal("10"),
    }
    # Input exists only as long-context and as flex, so nothing standard resolves and
    # the model is refused rather than charged at a long-context rate.
    assert select(card, regions=["us-east-1"], scope="geo") is None


def test_a_real_card_leaves_the_unparsed_counter_at_zero():
    """The operational contract in one assertion: on a real, current rate card, nothing
    is unparsed. If AWS renames a token dimension this goes nonzero, which is what makes
    `price_feed_unparsed_names` worth alerting on."""
    from mvp.pricing_feeds.agreement import AgreementFeed

    class _Client:
        def __init__(self, doc):
            self._doc = doc

        def list_foundation_model_agreement_offers(self, modelId):  # noqa: N803 — boto3 name
            return self._doc

    for fixture in ("agreement_opus5.json", "agreement_sonnet46.json",
                    "agreement_gpt56sol.json"):
        doc = json.loads((_FIXTURES / fixture).read_text())
        from mvp.pricing_feeds.base import FeedRequest

        result = AgreementFeed(_Client(doc)).fetch(
            FeedRequest(model_ids=frozenset({doc["modelId"]})))
        assert result.unparsed == 0, (fixture, result.unparsed_samples)
        assert result.cards, fixture
