"""Live price feeds for the rate table.

`mvp.price_sources` defines the seam: a *source* returns the whole
`{pricing_key: Rate}` table and is called on the pricing cache's refresh interval.
This package is one such source (`bedrock-live`) plus the feeds it reads:

- `dimensions` — the rate-name parsers and the selection rules every feed shares.
- `agreement` — `bedrock:ListFoundationModelAgreementOffers`, which is where every
  current Claude price and the OpenAI GPT-5.x prices actually live.
- `price_list` — the Price List API's `AmazonBedrock` offer, for the families AWS
  bills directly (Nova, Llama, Mistral, Qwen, Grok, Gemma, …).
- `selfhosted` — the operator's own cost-recovery document for the vLLM transport
  seam, so self-hosted capacity travels the same road as a provider price.

Nothing here is active by default. `register()` makes the name available and
`STRATOCLAVE_PRICE_SOURCE=bedrock-live` selects it, which keeps the default install
free of AWS calls on the pricing path — the same conservatism the money flags have.

Start with `docs/design/price-feeds.md`; it records what was measured about each API
and where each guarantee stops.
"""
from __future__ import annotations

from .base import Feed, FeedRequest, FeedResult
from .composite import NAME, FetchReport, LivePriceSource, register
from .dimensions import Selection, RateDimension, base_model_id, parse_agreement_dimension, \
    parse_price_list_usagetype, per_mtok, scope_for_model_id, select
from .snapshot import Snapshot, SnapshotStore, digest_of

__all__ = [
    "Feed",
    "FeedRequest",
    "FeedResult",
    "FetchReport",
    "LivePriceSource",
    "NAME",
    "Selection",
    "RateDimension",
    "Snapshot",
    "SnapshotStore",
    "base_model_id",
    "digest_of",
    "parse_agreement_dimension",
    "parse_price_list_usagetype",
    "per_mtok",
    "register",
    "scope_for_model_id",
    "select",
]
