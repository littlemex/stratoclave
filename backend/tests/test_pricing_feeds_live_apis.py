"""Do the real APIs still speak a grammar this build can read?

The offline tests parse fixtures captured on 2026-08-31, which proves the parsers are
right about what AWS emitted *then*. This is the test that fails when AWS changes it.
It is skipped without an opt-in flag, so the ordinary suite stays offline:

    STRATOCLAVE_LIVE_PRICE_TESTS=1 AWS_PROFILE=... pytest tests/test_pricing_feeds_live_apis.py

What it asserts is deliberately narrow. Not "the price is $5.50" — that is AWS's to
change and `tests/test_pricing_floor.py` is where a move gets noticed — but that the
pipeline still *resolves* the models this deployment serves, and that the names it
could not parse are all names that are not per-token prices in the first place.
"""
from __future__ import annotations

import os

import pytest

from mvp.models import registry_entries
from mvp.pricing_feeds.composite import LivePriceSource
from mvp.pricing_feeds.dimensions import base_model_id
from tests.live_aws import real_session

_FLAG = "STRATOCLAVE_LIVE_PRICE_TESTS"
# Model families whose price this deployment does not need from a feed: the
# self-hosted seam is priced by the operator, and a virtual router pool is never a
# charge of record.
_NOT_FEED_PRICED = {"vllm", "semantic-router"}
# Models known to have no readable price, with the reason. A ratchet in both
# directions: a model outside this set that stops being priced fails the first test,
# and a model inside it that starts being priced fails the second, so the list cannot
# quietly grow into a blanket excuse. Measured 2026-08-31 on account 776010787911;
# the authorization ones are account-scoped and may differ in yours.
EXPECTED_UNPRICED = {
    "anthropic.claude-3-opus-20240229-v1:0":
        "ListFoundationModelAgreementOffers rejects the id as invalid, and the Price "
        "List carries this model only under the AmazonBedrockFoundationModels offer, "
        "whose rows name their model by display name rather than by id",
}


class _NoStore:
    """Reads and writes nothing: a live parse check must not touch the snapshot the
    running deployment charges from."""

    def load(self):
        return None

    def save(self, rates, provenance):
        return None


@pytest.fixture(scope="module")
def report():
    if not os.getenv(_FLAG):
        pytest.skip(f"set {_FLAG}=1 (with AWS credentials) to check the live APIs")
    boto3 = pytest.importorskip("boto3")
    # `tests/conftest.py` plants dummy credentials so no test reaches AWS by accident;
    # a live check opts out of that by naming its profile and handing the clients in.
    from mvp.pricing_feeds.agreement import AgreementFeed
    from mvp.pricing_feeds.price_list import PriceListFeed
    from mvp.pricing_feeds.selfhosted import SelfHostedFeed

    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    region = os.getenv("STRATOCLAVE_REGION") or "us-east-1"
    feeds = (
        AgreementFeed(session.client("bedrock", region_name=region)),
        PriceListFeed(session.client("pricing", region_name="us-east-1")),
        SelfHostedFeed(),
    )
    source = LivePriceSource(feeds, store=_NoStore(), interval_seconds=0)
    result = source.refresh()
    if not result.rates and result.feed_errors:
        pytest.skip(f"price APIs unreachable with these credentials: {result.feed_errors}")
    return result


def test_the_registry_models_this_deployment_serves_are_still_priced(report):
    expected = {
        base_model_id(e.bedrock_model_id) for e in registry_entries()
        if not getattr(e, "virtual", False)
        and getattr(e, "served_by", "bedrock") not in _NOT_FEED_PRICED
    }
    # A model this account may not read is excluded: that is a permission to grant,
    # and counting it here would make the grammar check cry wolf on every deployment
    # whose account cannot read a legacy model's offer.
    unpriced = ({base_model_id(m) for m in report.unpriced}
                - {base_model_id(m) for m in report.unauthorized}
                - set(EXPECTED_UNPRICED)) & expected
    # A model AWS has not published a price for at all is the operator's problem, not
    # a parser bug — but a model that WAS priced and now is not is exactly the
    # signature this test exists to catch, so the failure names them.
    assert not unpriced, (
        f"the live feeds no longer price {sorted(unpriced)}: {report.unpriced}. "
        f"Either the rate-name grammar changed (see mvp/pricing_feeds/dimensions.py) or the "
        f"model moved offers."
    )


def test_every_unparsed_name_is_a_product_that_is_not_priced_per_token(report):
    """`unparsed` is the change signal. It has to stay empty of token prices, or a
    renamed dimension would hide inside a count that is always nonzero."""
    samples = [s for names in report.unparsed_samples.values() for s in names]
    for name in samples:
        lowered = name.lower()
        assert any(marker in lowered for marker in (
            "reserved", "tpm", "provisioned", "modelunits", "model_units",
            "customization", "customisation", "storage", "unit=",
        )), (
            f"unparsed rate name {name!r} looks like a per-token price this build "
            f"cannot read. Teach mvp/pricing_feeds/dimensions.py the new shape."
        )


def test_a_key_is_never_published_with_a_zero_leg(report):
    for key, rate in report.rates.items():
        assert rate.input_per_mtok_microusd > 0, key
        assert rate.output_per_mtok_microusd > 0, key
        assert rate.cache_read_per_mtok_microusd >= 0, key
        assert rate.cache_write_per_mtok_microusd >= 0, key


def test_the_expected_unpriced_list_has_not_gone_stale(report):
    """The other half of the ratchet: a model AWS has started publishing must come off
    the list, or the list becomes a blanket excuse that hides the next regression."""
    priced = {base_model_id(m) for m in report.rates} if report.rates else set()
    unpriced = {base_model_id(m) for m in report.unpriced}
    stale = sorted(set(EXPECTED_UNPRICED) - unpriced - set(report.unauthorized))
    # `report.rates` is keyed by pricing key, not by model, so absence from `unpriced`
    # is the signal that a model became readable.
    assert not stale, (
        f"these models are priced now and should be removed from EXPECTED_UNPRICED: "
        f"{stale}"
    )
    assert not (set(EXPECTED_UNPRICED) & priced)
