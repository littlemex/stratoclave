"""E3 — the three data-derived gates.

Each gate is a named callable that answers one question about ONE model with
`Optional[Blocker]`: `None` means the gate found nothing wrong, a `Blocker`
names what it found and why. Every `Blocker` built below omits `first_seen`/
`last_seen`, so the constructor defaults both to the same "now" — a gate has
no memory of a previous pass, so that is the only honest timestamp it could
give anyway. See `mvp.discovery.records.merge_blockers` for how a blocker's
original `first_seen` survives across reconciliation runs once one exists.

Tests call these directly. None of them reimplements a check that lives
elsewhere: `gate_output_is_text` reads a field `GetFoundationModel` already
returns, and `gate_agreement_exists` / `gate_card_prices_tokens` read
`bedrock:ListFoundationModelAgreementOffers`'s own rate card through
`pricing_feeds.dimensions`'s already-reviewed parsers (`parse_agreement_dimension`,
`per_mtok`) — the same ones `pricing_feeds.agreement` uses to build the pricing
snapshot. Writing a second parser here would be a second answer to a question
that module already answers correctly.

`Agreement not supported for this model` means the price is not discoverable
through this API — that is one of two things `gate_agreement_exists` can find
wrong, and it is why `gate_agreement_exists` and `gate_card_prices_tokens` are
two DIFFERENT gates rather than one: a model can fail `gate_agreement_exists`
(no card came back at all) or pass it and still fail `gate_card_prices_tokens`
(a card came back, none of it prices tokens — Stability's rate card is
exactly this: four dimensions, zero of them per-token). Collapsing the two
would lose the distinction the evidence needs to keep: "we could not read a
price" is not the same fact as "we read one, and it does not charge for
tokens."

The settled per-gate mapping, five blocker types, three gates:

  - `gate_output_is_text`      -> `unsupported_output_modality`
  - `gate_agreement_exists`    -> `no_agreement_offer` when
                                   `ListFoundationModelAgreementOffers` answers
                                   "Agreement not supported for this model"
                                   (the mechanism does not exist for this
                                   model), and `no_model_access` when it
                                   answers "not authorized to invoke this API
                                   operation" (the mechanism exists and this
                                   account has not been granted it — the fix
                                   is a click in the provider console, which
                                   is never the fix for the first case). Both
                                   are answers the SAME call already gives
                                   distinctly; neither needs a probe.
  - `gate_card_prices_tokens`  -> `no_token_pricing`
  - an unparseable rate-card dimension -> `price_dimensions_unknown`, minted
                                   by `mvp.discovery.reconcile`, never here (a
                                   `Selection` carries no memory of a row that
                                   failed to parse, so only the code doing the
                                   parsing can know).
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional, Sequence

from .records import Blocker
from ..pricing_feeds.agreement import _NOT_AUTHORIZED, _NOT_MARKETPLACE
from ..pricing_feeds.base import STRATOCLAVE_REGION_ENV
from ..pricing_feeds.dimensions import EXCLUDED, parse_agreement_dimension, per_mtok

_DEFAULT_REGION = "us-east-1"


def gate_output_is_text(model_details: Mapping[str, Any]) -> Optional[Blocker]:
    """`outputModalities` must include `TEXT`.

    `model_details` is the dict `bedrock:GetFoundationModel` answers (the
    `modelDetails` field's own contents — `outputModalities`, `inputModalities`,
    `providerName`, ... — already unwrapped by the caller). A model whose
    output is not `TEXT` (Stability: `IMAGE` in / `IMAGE` out) is not a model
    this account has failed to be granted — no console click ever changes what
    a model outputs — so this is `unsupported_output_modality`, never
    `no_model_access`. It blocks the model regardless of what its rate card
    says, which is a different, coarser fact than any pricing gate below.
    """
    modalities = model_details.get("outputModalities") or []
    if "TEXT" in modalities:
        return None
    return Blocker(
        type="unsupported_output_modality", subtype="output_not_text",
        evidence=f"outputModalities={sorted(str(m) for m in modalities)!r}",
    )


def _bedrock_client(client: Optional[Any] = None, *, region: Optional[str] = None):
    if client is not None:
        # Injected (tests, `reconcile.py`'s own single shared client): this gate
        # does not own timeouts or retry policy, and must not silently override
        # what the caller built — same convention as every feed in
        # `pricing_feeds` (`AgreementFeed._bedrock`, `PriceListFeed._pricing`).
        return client
    import boto3

    return boto3.client(
        "bedrock", region_name=region or os.getenv(STRATOCLAVE_REGION_ENV) or _DEFAULT_REGION
    )


def fetch_rate_card(
    model_id: str, *, client: Optional[Any] = None, region: Optional[str] = None,
) -> tuple[Optional[list[dict]], Optional[Blocker]]:
    """The raw `rateCard` rows `bedrock:ListFoundationModelAgreementOffers`
    publishes for `model_id`, or the blocker that explains why there are none.

    Exposed (not private) so a caller that also needs `gate_card_prices_tokens`'s
    input — `reconcile.py`, and any test exercising both gates against one
    observed call — asks the API once rather than twice: `gate_agreement_exists`
    below is a thin wrapper over this that keeps the rows.

    Four outcomes, matching `pricing_feeds.agreement`'s own reading of this
    API, and the first two are DIFFERENT blocker types on purpose (see the
    module docstring's settled mapping): the call fails with "agreement not
    supported" — `no_agreement_offer`, `_NOT_MARKETPLACE`; this model is not
    Marketplace-metered, a fact about the model, not evidence it has no price
    anywhere — or the call fails with "not authorized" — `no_model_access`,
    `_NOT_AUTHORIZED`; an account permission this account has not been
    granted, reusing `pricing_feeds.agreement`'s own constant rather than
    respelling AWS's error text a second time. A client that could not even be
    built, or a call that fails some other way, or one that succeeds with no
    `rateCard` row anywhere, all still read as `no_agreement_offer`: none of
    them is evidence that access is the problem.
    """
    try:
        bedrock = _bedrock_client(client, region=region)
    except Exception as exc:  # noqa: BLE001 — no client, no card; not fatal.
        return None, Blocker(type="no_agreement_offer", subtype="client_unavailable",
                             evidence=str(exc))
    try:
        response = bedrock.list_foundation_model_agreement_offers(modelId=model_id)
    except Exception as exc:  # noqa: BLE001 — see the module contract.
        message = str(exc)
        lowered = message.lower()
        if _NOT_MARKETPLACE in lowered:
            return None, Blocker(type="no_agreement_offer", subtype="not_marketplace_metered",
                                 evidence=message)
        if _NOT_AUTHORIZED in lowered:
            return None, Blocker(type="no_model_access", subtype="not_authorized",
                                 evidence=message)
        return None, Blocker(type="no_agreement_offer", subtype="call_failed", evidence=message)
    rows: list[dict] = []
    offers = response.get("offers") if isinstance(response, Mapping) else None
    for offer in offers or ():
        if not isinstance(offer, Mapping):
            continue
        term = offer.get("termDetails") or {}
        usage_term = term.get("usageBasedPricingTerm") if isinstance(term, Mapping) else None
        card_rows = (usage_term or {}).get("rateCard") if isinstance(usage_term, Mapping) else None
        if isinstance(card_rows, (list, tuple)):
            rows.extend(row for row in card_rows if isinstance(row, Mapping))
    if not rows:
        return None, Blocker(
            type="no_agreement_offer", subtype="empty_rate_card",
            evidence="the call succeeded but offers[].termDetails.usageBasedPricingTerm."
            "rateCard carried no rows",
        )
    return rows, None


def gate_agreement_exists(model_id: str, *, client: Optional[Any] = None) -> Optional[Blocker]:
    """A usable rate card must be obtainable from
    `bedrock:ListFoundationModelAgreementOffers`. See `fetch_rate_card` for
    what "usable" means here, and for why its failure shapes split across
    `no_agreement_offer` and `no_model_access` rather than sharing one type.
    """
    _, blocker = fetch_rate_card(model_id, client=client)
    return blocker


def gate_card_prices_tokens(rate_card: Sequence[Mapping[str, Any]]) -> Optional[Blocker]:
    """At least one dimension in `rate_card` must resolve to a per-token price.

    `rate_card` is the row list `fetch_rate_card` returns (or, for a test, the
    same shape built by hand): each row is `{dimension, price, description,
    unit}`. A row is skipped without counting against the model — never
    without being looked at — when `parse_agreement_dimension` reports it is
    `EXCLUDED` (reserved/provisioned throughput, a non-default cache TTL) or
    when it is a `long_ctx` / non-`standard`-mode dimension, because none of
    those are a token price this gateway would ever charge from; a row this
    build cannot parse at all (`None`) is ALSO skipped here — `gate_
    card_prices_tokens` only asks "is there at least one usable row", not "is
    every row accounted for", which is `mvp.discovery.pricing_key`'s stricter
    question.
    """
    for row in rate_card or ():
        dimension = row.get("dimension") if isinstance(row, Mapping) else None
        if not dimension:
            continue
        parsed = parse_agreement_dimension(dimension)
        if parsed is EXCLUDED or parsed is None:
            continue
        _, slot = parsed
        if slot.long_ctx or slot.mode != "standard":
            continue
        unit = row.get("unit")
        if not unit:
            continue
        if per_mtok(row.get("price"), unit) is None:
            continue
        return None
    dims = sorted({
        str(row.get("dimension")) for row in (rate_card or ()) if isinstance(row, Mapping)
        and row.get("dimension")
    })
    return Blocker(
        type="no_token_pricing", subtype="zero_token_priced_dimensions",
        evidence=f"{len(dims)} dimension(s), none priced per token: {dims[:10]!r}",
    )
