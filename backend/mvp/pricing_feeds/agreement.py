"""The Marketplace-metered feed: `bedrock:ListFoundationModelAgreementOffers`.

This is where the price of every current Claude model lives, and it is not where
anyone looks first. The AWS Price List API publishes Bedrock prices under four
offer codes, and the current Anthropic generation appears in none of the obvious
ones: `AmazonBedrock` stops at Claude 3 Sonnet, `AmazonBedrockService` covers
Sonnet 4 / 4.5 / Haiku 4.5 and reserved throughput, and `AmazonBedrockFoundation
Models` does carry the rest but as anonymous "AWS Marketplace software usage" rows
whose model is only identifiable through a `servicename` attribute. The OpenAI
GPT-5.x family is absent from all four in commercial regions.

`ListFoundationModelAgreementOffers` answers all of them directly, as a structured
rate card:

    aws bedrock list-foundation-model-agreement-offers --model-id anthropic.claude-opus-5
    -> offers[0].termDetails.usageBasedPricingTerm.rateCard[]
       { "dimension": "USW2_input_tokens_global_standard", "price": "5", "unit": "Units" }

Measured properties this module relies on (2026-08-31, real API):

- The card is **region-independent**: calling from us-west-2 and from
  ap-northeast-1 returns the same `offerId` and byte-identical dimensions. Region
  lives inside the dimension names, so one call prices every region.
- It covers the Marketplace-metered families — Anthropic (all generations), OpenAI
  GPT-5.6 sol / terra / luna, Cohere, Stability, Writer, TwelveLabs, Luma — and
  answers `ValidationException` for the AWS-billed ones (Nova, Titan, Llama,
  Mistral, DeepSeek, Gemma, Qwen, NVIDIA, MiniMax, gpt-oss, xAI). That exception is
  therefore **information, not failure**: it is how the split between this feed and
  the Price List feed is decided at runtime instead of being hardcoded.
- Prices are USD per million tokens, reported with `unit: "Units"`.

One thing this module must never do: the response also carries an `offerToken`,
which is the signed blob `CreateFoundationModelAgreement` consumes. Reading prices
must not become subscribing to a paid product, so the token is dropped on the floor
here and never stored, logged, or returned.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Optional

from core.logging import get_logger

from .base import STRATOCLAVE_REGION_ENV, Card, FeedRequest, FeedResult
from .dimensions import EXCLUDED, parse_agreement_dimension, per_mtok

logger = get_logger(__name__)

NAME = "bedrock-agreement"

# The API is region-independent (see the module docstring), so the client region is
# an operational choice, not a pricing one: use the deployment's own region so the
# call stays inside it.
_REGION_ENV = STRATOCLAVE_REGION_ENV
_DEFAULT_REGION = "us-east-1"

# One call per model, and a registry of twenty models took ~40 s in sequence against
# real Bedrock — long enough that a fetch on the request path hit its budget with most
# models unasked, and the partial table then repeated the same prefix every pass. The
# calls are independent, so they run on a small pool instead. Bounded: this is a
# background refresh, not a workload, and a wide pool against a price API is a good way
# to get throttled. botocore's low-level clients are documented as safe to share
# between threads (unlike sessions and resources), and this feed only ever reads.
WORKERS_ENV = "STRATOCLAVE_PRICE_FEED_WORKERS"
DEFAULT_WORKERS = 8

# AWS's way of saying "this model is not Marketplace-metered". Matched on the
# message because the error code is the generic `ValidationException`, which also
# covers a genuinely bad model id — a distinction worth keeping, since a typo in
# the registry should look like an error and not like "another feed's problem".
_NOT_MARKETPLACE = "agreement not supported"
# Measured on real Bedrock: the current generations answer, while several legacy
# Claude models come back "Your account is not authorized to invoke this API
# operation" for the same call. That is an account fact, not a broken feed, so those
# models are reported as such and keep whatever the layer below charges.
_NOT_AUTHORIZED = "not authorized to invoke this api operation"


class AgreementFeed:
    """Reads rate cards for the Marketplace-metered families."""

    name = NAME

    def __init__(self, client=None, *, region: Optional[str] = None) -> None:
        self._client = client
        self._region = region or os.getenv(_REGION_ENV) or _DEFAULT_REGION

    def _bedrock(self, request: FeedRequest):
        if self._client is not None:
            # Injected (tests, an embedding caller): this feed does not own its
            # timeouts and must not silently override what the caller built.
            return self._client
        import boto3
        from botocore.config import Config

        # Built fresh per pass, sized to THIS pass's remaining budget, rather than
        # cached: the composite calls `fetch()` with a different budget for a
        # request-path refresh than for the ops CLI's `refresh()`, and a client
        # cached from the first call would carry the wrong one for every pass after
        # it. `standard` retry mode backs off on throttling without also retrying a
        # call that is already the reason a pass is late; two attempts bounds the
        # worst case to roughly twice the per-call timeout instead of botocore's
        # default of many.
        timeout = request.remaining_seconds()
        # `total_max_attempts` (not `max_attempts`, which counts RETRIES after the
        # initial call, so `max_attempts=2` would mean three attempts) is also what
        # botocore prefers, since it is the same key `AWS_MAX_ATTEMPTS` sets.
        config = Config(connect_timeout=timeout, read_timeout=timeout,
                        retries={"total_max_attempts": 2, "mode": "standard"})
        return boto3.client("bedrock", region_name=self._region, config=config)

    def fetch(self, request: FeedRequest) -> FeedResult:
        result = FeedResult()
        try:
            client = self._bedrock(request)
        except Exception as exc:  # noqa: BLE001 — no client, no prices; not fatal.
            result.note_error(f"cannot construct bedrock client: {exc}")
            return result
        wanted = sorted(request.model_ids)
        skipped = 0
        lock = threading.Lock()

        def one(model_id: str) -> None:
            nonlocal skipped
            if request.out_of_time():
                with lock:
                    skipped += 1
                return
            try:
                response = client.list_foundation_model_agreement_offers(modelId=model_id)
            except Exception as exc:  # noqa: BLE001 — see the module contract.
                message = str(exc)
                lowered = message.lower()
                with lock:
                    if _NOT_MARKETPLACE in lowered:
                        result.out_of_scope.add(model_id)
                    elif _NOT_AUTHORIZED in lowered:
                        result.not_authorized.add(model_id)
                    else:
                        result.note_model_error(model_id, message)
                    if request.out_of_time():
                        # The call started before the deadline (or it would have been
                        # skipped above) but the deadline passed while it was in
                        # flight — a stall the client's own `Config` timeout eventually
                        # cut off. The pass ran longer than its budget even though
                        # nothing here was skipped, and that must read the same as a
                        # skip: `truncated` is about how long the pass took, not about
                        # which calls it declined to make.
                        result.truncated = True
                return
            with lock:
                card = _parse_offers(response, result)
                if card:
                    result.cards[model_id] = card
                else:
                    # Reachable, answered, and yet nothing usable came out. That is the
                    # shape a renamed grammar takes, so it is an error rather than a
                    # silent empty.
                    result.note_model_error(
                        model_id, "rate card produced no chargeable slot")
                if request.out_of_time():
                    # Same reasoning as the exception branch: a call that returns
                    # (successfully, this time) after the deadline still means the pass
                    # overran its budget.
                    result.truncated = True

        workers = max(1, min(_worker_count(), len(wanted) or 1))
        if workers == 1:
            for model_id in wanted:
                one(model_id)
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="price-agreement") as pool:
                list(pool.map(one, wanted))
        if skipped:
            # A skipped model produced no card at all, so the fold already treats its key
            # as incomplete; the flag is for the report and the deploy gate.
            result.truncated = True
            result.note_error(f"stopped at the fetch budget with {skipped} model(s) unasked")
        return result


def _parse_offers(response: object, result: FeedResult) -> Card:
    card: Card = {}
    offers = _get(response, "offers") or []
    if not isinstance(offers, (list, tuple)):
        result.note_error("offers is not a list")
        return card
    for offer in offers:
        term = _get(_get(offer, "termDetails") or {}, "usageBasedPricingTerm") or {}
        rows = _get(term, "rateCard") or []
        if not isinstance(rows, (list, tuple)):
            result.note_error("rateCard is not a list")
            continue
        for row in rows:
            dimension = _get(row, "dimension")
            parsed = parse_agreement_dimension(dimension) if dimension else None
            if parsed is EXCLUDED:
                # Recognised, and not a price this gateway charges. Skipped without
                # touching `unparsed`, which exists to say "a token price changed shape".
                continue
            if parsed is None:
                if dimension:
                    result.note_unparsed(dimension)
                continue
            unit = _get(row, "unit")
            if not unit:
                # No unit, no price. This module's own rule is that an unknown unit is
                # dropped because assuming the wrong one is a 1000-fold error; assuming
                # one for a MISSING unit is the same bet with less evidence.
                result.note_unparsed(f"{dimension} (no unit)")
                continue
            value = per_mtok(_get(row, "price"), unit)
            if value is None:
                result.note_unparsed(f"{dimension} (price={_get(row, 'price')!r})")
                continue
            key = parsed
            # Two offers for one model would be an ambiguity we cannot resolve, so
            # the dearer number wins: the ledger's standing rule is that an
            # uncertain price over-charges rather than under-charges.
            previous = card.get(key)
            if previous is None or value > previous:
                card[key] = value
    return card


def _get(obj: object, field: str):
    """Field access that tolerates both a dict and an object.

    boto3 returns dicts today. A future client (or a test double) returning objects
    should not require a second parser, and neither should crash this one.
    """
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def usd_per_mtok_debug(card: Card) -> dict[str, str]:
    """Flatten a card for logging / the ops CLI. Not used on the request path."""
    return {
        f"{region or 'any'}|{slot.token_class}|{slot.scope}"
        f"{'|lctx' if slot.long_ctx else ''}|{slot.mode}": str(value)
        for (region, slot), value in sorted(
            card.items(), key=lambda kv: (str(kv[0][0]), kv[0][1].token_class)
        )
        if isinstance(value, Decimal)
    }


def _worker_count() -> int:
    raw = os.getenv(WORKERS_ENV)
    if raw:
        try:
            value = int(raw)
            if value >= 1:
                return value
        except ValueError:
            pass
    return DEFAULT_WORKERS
