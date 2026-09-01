"""The AWS-billed feed: the Price List API's `AmazonBedrock` offer.

The families `ListFoundationModelAgreementOffers` refuses — Nova, Titan, Llama,
Mistral, DeepSeek, Gemma, Qwen, NVIDIA, MiniMax, GLM, Kimi, gpt-oss, xAI — are the
ones AWS bills directly, and those are published here, as usage types:

    USE1-xai.grok-4.6-mantle-input-tokens-global-standard   $0.0020 per 1K tokens
    USE1-nvidia.nemotron-super-3-120b-mantle-output-tokens-standard

Two things about this source are easy to get wrong and are handled explicitly:

- **The unit is per 1K tokens here**, while the agreement rate cards are per 1M. The
  same model can appear in both. `dimensions.per_mtok` normalises by the unit string and
  drops a unit it does not know, because assuming the wrong one is a 1000x error.
- **The `model` attribute is not a model id.** It holds a display name for some
  models (`Claude 3 Haiku`, `Nova Pro`) and a raw id for others (`xai.grok-4.6`),
  so this feed keys on the `usagetype` string, which always embeds the id, and
  requires the registry's id as the anchor.

Only the `standard` tier is charged (see `dimensions.MODES`), so `flex`, `priority` and
`batch` rows are parsed, recognised, and discarded rather than silently averaged in.
"""
from __future__ import annotations

import json
import os
from typing import Iterable, Optional

from core.logging import get_logger

from .base import STRATOCLAVE_REGION_ENV, Card, FeedRequest, FeedResult
from .dimensions import EXCLUDED, parse_price_list_usagetype, per_mtok

logger = get_logger(__name__)

NAME = "bedrock-price-list"

# The Price List API is only served from a few regions; it is a global catalogue, so
# the endpoint is an availability choice and never affects the answer.
ENDPOINT_REGION_ENV = "STRATOCLAVE_PRICE_LIST_REGION"
_DEFAULT_ENDPOINT_REGION = "us-east-1"
# The deployment's own region, named once in `base.py`. `agreement.py` reads the
# same constant.
REGION_ENV = STRATOCLAVE_REGION_ENV
# Bedrock prices are spread across several offer codes and a model can move between
# them: `AmazonBedrockService` carries Sonnet 4 / 4.5 / Haiku 4.5 while `AmazonBedrock`
# carries the mantle families. Reading more than one costs pages, not correctness — a row
# only matches when its usage type anchors on a model id we asked about — so the list is
# configurable rather than a single constant that quietly misses a migration.
OFFERS_ENV = "STRATOCLAVE_PRICE_LIST_OFFERS"
DEFAULT_OFFERS = ("AmazonBedrock", "AmazonBedrockService")
# One page is 100 products and a region holds ~1000, so a handful of pages per region.
# Bounded so a catalogue that grows by an order of magnitude cannot turn a price refresh
# into an unbounded scan; env-tunable because that bound is an operational judgement.
MAX_PAGES_ENV = "STRATOCLAVE_PRICE_LIST_MAX_PAGES"
DEFAULT_MAX_PAGES_PER_REGION = 40


class PriceListFeed:
    """Reads per-token rates for the AWS-billed families, region by region."""

    name = NAME

    def __init__(self, client=None, *, endpoint_region: Optional[str] = None,
                 regions: Optional[Iterable[str]] = None) -> None:
        self._client = client
        self._endpoint_region = (endpoint_region or os.getenv(ENDPOINT_REGION_ENV)
                                 or _DEFAULT_ENDPOINT_REGION)
        # Which regions to ask about. Left to the caller (the composite passes the
        # deployment's candidate regions) so a single-region deployment does not
        # download the whole world's prices on every refresh.
        self._regions = tuple(regions or ())

    def _pricing(self, request: FeedRequest):
        if self._client is not None:
            # Injected (tests, an embedding caller): leave whatever timeout the
            # caller built it with alone.
            return self._client
        import boto3
        from botocore.config import Config

        # Same reasoning as `AgreementFeed._bedrock`: built fresh per pass, sized to
        # this pass's remaining budget, rather than cached across passes that may
        # carry a different budget.
        timeout = request.remaining_seconds()
        # Same key choice as `AgreementFeed._bedrock`: `total_max_attempts`, not
        # `max_attempts`, which counts retries after the initial call rather than the
        # total.
        config = Config(connect_timeout=timeout, read_timeout=timeout,
                        retries={"total_max_attempts": 2, "mode": "standard"})
        return boto3.client("pricing", region_name=self._endpoint_region, config=config)

    def fetch(self, request: FeedRequest) -> FeedResult:
        model_ids = request.model_ids
        result = FeedResult()
        if not model_ids:
            return result
        try:
            client = self._pricing(request)
        except Exception as exc:  # noqa: BLE001
            result.note_error(f"cannot construct pricing client: {exc}")
            return result
        regions = (request.regions or self._regions
                   or (os.getenv(REGION_ENV) or _DEFAULT_ENDPOINT_REGION,))
        for region in sorted({r for r in regions if r}):
            if request.out_of_time():
                result.truncated = True
                result.incomplete_models.update(model_ids)
                result.note_error("stopped at the fetch budget with regions unread")
                break
            for offer in _offers():
                if request.out_of_time():
                    result.truncated = True
                    result.incomplete_models.update(model_ids)
                    result.note_error(
                        f"{region}: stopped at the fetch budget before offer {offer}")
                    break
                self._fetch_region(client, offer, region, model_ids, result, request)
        for model_id in sorted(model_ids):
            if model_id in result.cards or model_id in result.incomplete_models:
                # A card answers for it, or this pass already said it does not know
                # whether the model is ours — either way "not our business" would be
                # false. Only a model this scan actually got all the way through
                # without ever finding a row for it is established as out of scope.
                continue
            result.out_of_scope.add(model_id)
        return result

    def _fetch_region(self, client, offer: str, region: str, model_ids: frozenset[str],
                     result: FeedResult, request: FeedRequest) -> None:
        token = None
        pages = 0
        max_pages = _max_pages()
        while pages < max_pages:
            pages += 1
            if request.out_of_time():
                result.truncated = True
                result.incomplete_models.update(model_ids)
                result.note_error(f"{offer}/{region}: stopped at the fetch budget after "
                                  f"{pages - 1} page(s)")
                return
            kwargs = {
                "ServiceCode": offer,
                "Filters": [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}],
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            try:
                response = client.get_products(**kwargs)
            except Exception as exc:  # noqa: BLE001 — see the feed contract.
                # This region is now unread, so every model's answer may be missing the
                # region it is dearest in. Saying so is what keeps the fold from reading a
                # smaller maximum as a price cut.
                result.truncated = True
                result.incomplete_models.update(model_ids)
                result.note_error(f"{offer}/{region}: get_products failed: {exc}")
                return
            if request.out_of_time():
                # The call returned (successfully) after the deadline had already
                # passed — a stall the client's own `Config` timeout eventually cut
                # off. Nothing here was skipped, but the pass still ran longer than
                # its budget, and that must read the same as a skip.
                result.truncated = True
                result.incomplete_models.update(model_ids)
                result.note_error(
                    f"{offer}/{region}: get_products returned after the fetch budget")
            for raw in response.get("PriceList", ()):
                _absorb_product(raw, model_ids, result)
            token = response.get("NextToken")
            if not token:
                return
        result.truncated = True
        result.incomplete_models.update(model_ids)
        result.note_error(f"{offer}/{region}: stopped after {max_pages} pages")


def _absorb_product(raw: object, model_ids: frozenset[str], result: FeedResult) -> None:
    try:
        product = json.loads(raw) if isinstance(raw, str) else raw
        attributes = product["product"]["attributes"]
        usagetype = attributes.get("usagetype") or ""
    except Exception as exc:  # noqa: BLE001 — one malformed product is not fatal.
        result.note_error(f"unparseable product: {exc}")
        return
    if not usagetype:
        return
    # Longest id first: two registry ids can be prefixes of one another, and the
    # longer one is the specific model the row is about.
    for model_id in sorted(model_ids, key=len, reverse=True):
        parsed = parse_price_list_usagetype(usagetype, model_id)
        if parsed is EXCLUDED:
            return          # recognised, not charged, and not a grammar problem
        if parsed is None:
            if _names_model(usagetype, model_id):
                # The row is about one of OUR models and could not be read: that is the
                # signal a grammar changed, and dropping it silently is how a leg freezes.
                result.note_unparsed(usagetype)
                return
            continue
        region, slot = parsed
        for value in _prices(product, usagetype, result):
            card = result.cards.setdefault(model_id, {})
            key = (region, slot)
            previous = card.get(key)
            if previous is None or value > previous:
                card[key] = value
        return


def _prices(product: object, usagetype: str, result: FeedResult):
    terms = (product.get("terms") or {}).get("OnDemand") or {}
    for term in terms.values():
        for dimension in (term.get("priceDimensions") or {}).values():
            usd = (dimension.get("pricePerUnit") or {}).get("USD")
            value = per_mtok(usd, dimension.get("unit") or "")
            if value is None:
                result.note_unparsed(f"{usagetype} (unit={dimension.get('unit')!r})")
                continue
            yield value


def _offers() -> tuple[str, ...]:
    raw = os.getenv(OFFERS_ENV)
    if raw:
        offers = tuple(o.strip() for o in raw.split(",") if o.strip())
        if offers:
            return offers
    return DEFAULT_OFFERS


def _max_pages() -> int:
    raw = os.getenv(MAX_PAGES_ENV)
    if raw:
        try:
            value = int(raw)
            if value >= 1:
                return value
        except ValueError:
            pass
    return DEFAULT_MAX_PAGES_PER_REGION


def _names_model(usagetype: str, model_id: str) -> bool:
    """Is this usage type about `model_id` at all?

    Used to tell "another model's row" from "our model's row in a shape we cannot read".
    The first is noise; the second is the only warning that a grammar changed.
    """
    _, _, rest = usagetype.partition("-")
    return rest.startswith(model_id) and rest[len(model_id):].startswith("-")
