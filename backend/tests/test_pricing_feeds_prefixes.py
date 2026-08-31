"""Is the bundled billing-prefix table still what AWS publishes?

`dimensions.BILLING_PREFIXES` maps `USE1` to `us-east-1` and so on, and it is load-bearing:
an agreement rate card names its regions only by that prefix, so a wrong entry would
either drop a region's price or attribute it to the wrong one.

The table is not a guess at the naming convention — it is a projection of AWS's own
data, because every Bedrock product in the Price List carries both `regionCode` and a
`usagetype` beginning with the prefix. This test rebuilds the projection against the
live API and compares. It is skipped without credentials (and in CI by default), so
the offline suite never depends on the network:

    STRATOCLAVE_LIVE_PRICE_TESTS=1 pytest tests/test_pricing_feeds_prefixes.py

The companion live check for the rate-name grammars is
`tests/test_pricing_feeds_live_apis.py`.
"""
from __future__ import annotations

import json
import os

import pytest

from mvp.pricing_feeds.dimensions import BILLING_PREFIXES
from tests.live_aws import real_session

_FLAG = "STRATOCLAVE_LIVE_PRICE_TESTS"
_OFFERS = ("AmazonBedrock", "AmazonBedrockFoundationModels", "AmazonBedrockService")


def _live_or_skip():
    if not os.getenv(_FLAG):
        pytest.skip(f"set {_FLAG}=1 to check the prefix table against the live API")
    boto3 = pytest.importorskip("boto3")
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    return session.client("pricing", region_name="us-east-1")


def test_bundled_prefixes_match_the_live_price_list():
    client = _live_or_skip()
    seen: dict[str, dict[str, int]] = {}
    for service in _OFFERS:
        token = None
        while True:
            kwargs = {"ServiceCode": service, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            response = client.get_products(**kwargs)
            for raw in response.get("PriceList", ()):
                attributes = json.loads(raw)["product"]["attributes"]
                region = attributes.get("regionCode")
                usagetype = attributes.get("usagetype") or ""
                prefix = usagetype.split("-", 1)[0]
                if not region or not prefix.isalnum() or not prefix.isupper():
                    continue
                seen.setdefault(prefix, {}).setdefault(region, 0)
                seen[prefix][region] += 1
            token = response.get("NextToken")
            if not token:
                break

    live = {prefix: max(regions.items(), key=lambda kv: kv[1])[0]
            for prefix, regions in seen.items()}
    # A prefix AWS has added is a gap to fill, not a failure of the existing rows:
    # the bundled table only has to be RIGHT about what it claims, and complete enough
    # to cover the regions this deployment can dispatch to.
    wrong = {p: (BILLING_PREFIXES[p], live[p]) for p in set(BILLING_PREFIXES) & set(live)
             if BILLING_PREFIXES[p] != live[p]}
    assert not wrong, (
        f"bundled prefix -> region disagrees with the live Price List: {wrong}. "
        f"Regenerate with `python -m mvp.pricing_feeds.fetch --print-prefixes`."
    )
    missing = sorted(set(live) - set(BILLING_PREFIXES))
    if missing:
        pytest.skip(f"AWS has prefixes this build does not know yet: {missing} "
                    f"(regenerate the document to price those regions)")
