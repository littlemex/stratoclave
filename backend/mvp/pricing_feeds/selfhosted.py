"""The self-hosted feed: cost recovery for capacity the operator owns.

The transport seam (`served_by="vllm"`, and the semantic-router pool entries that
execute on it) puts a self-hosted GPU behind the same reserve / rate / settle path as
Bedrock. Its price is not a list price — nobody publishes it — so it cannot come
from an API, and pretending otherwise would put an invented number on the charge of
record. It comes from a document the operator writes, and this feed exists so that
number travels the *same* road as a provider price: parsed once, snapshotted,
labelled with its provenance, merged under the same precedence, and visible in the
same admin view. One pipeline, three sources.

The document is keyed by `endpoint_key`, not by model or pricing key, because cost
recovery is a property of the capacity: one vLLM pool serves several models and its
dollars-per-hour does not change when a new model is loaded onto it.

    {
      "schema_version": 1,
      "rates": {
        "pool-a": {"input_per_mtok_usd": "0.20", "output_per_mtok_usd": "0.20",
                   "notes": "g6e.2xlarge reserved, amortised at the occupancy we measured"}
      }
    }

Two guarantees this feed enforces rather than documents:

- **Cache rates are zero.** vLLM reports no Bedrock-style cache-token split, so any
  nonzero cache rate would be dead pricing that also skews the router's warm-prefix
  delta. `mvp.pricing` clamps this on the money path as well; the feed does not rely
  on that clamp, because a feed that publishes a number it knows is wrong is a bug
  even when something downstream fixes it.
- **The gateway does not derive the rate.** It will not turn an hourly cost and a
  guessed throughput into a per-token price. Occupancy is a measurement, and a
  measured number belongs to whoever measured it; a per-token rate derived from a
  latency figure is exactly how a cost model ends up an order of magnitude out.
  `docs/design/price-feeds.md` says how to compute it and what it means.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Optional

from core.logging import get_logger

from .base import Card, FeedRequest, FeedResult
from .dimensions import SCOPES, RateDimension

logger = get_logger(__name__)

NAME = "self-hosted"

PATH_ENV = "STRATOCLAVE_SELFHOSTED_RATES_PATH"
_SUPPORTED_SCHEMA_VERSION = 1
_DOC_FIELDS = frozenset({"schema_version", "rates", "$comment"})
_ENTRY_FIELDS = frozenset({"input_per_mtok_usd", "output_per_mtok_usd", "notes"})


class SelfHostedFeed:
    """Publishes operator-declared cost-recovery rates for self-hosted endpoints."""

    name = NAME

    def __init__(self, path: Optional[str] = None, *, registry=None) -> None:
        self._path = path
        self._registry = registry

    def _entries(self):
        if self._registry is not None:
            return self._registry
        from ..models import registry_entries

        return registry_entries()

    def _document_path(self) -> Optional[str]:
        return self._path or os.getenv(PATH_ENV) or None

    def fetch(self, request: FeedRequest) -> FeedResult:
        # The deadline is irrelevant here: this feed reads a local document, so there is
        # no remote call to give up on.
        model_ids = request.model_ids
        result = FeedResult()
        # Which of the asked-about models are self-hosted at all. A Bedrock model is
        # not this feed's business, and saying so lets the composite tell "no feed
        # owns this model" from "this feed had nothing to say".
        by_endpoint: dict[str, list[str]] = {}
        for entry in self._entries():
            if entry.bedrock_model_id not in model_ids:
                continue
            if getattr(entry, "served_by", "bedrock") == "bedrock":
                result.out_of_scope.add(entry.bedrock_model_id)
                continue
            key = getattr(entry, "endpoint_key", None)
            if not key:
                # A semantic-router pool entry has no endpoint of its own; it is
                # priced at whatever the executed model costs, which is another
                # feed's answer.
                result.out_of_scope.add(entry.bedrock_model_id)
                continue
            by_endpoint.setdefault(key, []).append(entry.bedrock_model_id)
        if not by_endpoint:
            return result
        path = self._document_path()
        if not path:
            result.note_error(
                f"{len(by_endpoint)} self-hosted endpoint(s) are registered but "
                f"{PATH_ENV} is unset, so no cost-recovery rate is published"
            )
            return result
        try:
            rates = _load_document(path)
        except Exception as exc:  # noqa: BLE001 — a bad document must not stop charging.
            result.note_error(f"{path}: {exc}")
            return result
        for endpoint_key, ids in sorted(by_endpoint.items()):
            declared = rates.get(endpoint_key)
            if declared is None:
                result.note_error(f"{path}: no rate declared for endpoint {endpoint_key!r}")
                continue
            card = _card_for(declared)
            for model_id in ids:
                result.cards[model_id] = dict(card)
        return result


def _load_document(path: str) -> dict[str, dict[str, Decimal]]:
    from ..rates import no_duplicate_keys

    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh, object_pairs_hook=no_duplicate_keys)
    if not isinstance(doc, dict):
        raise ValueError("top level must be an object")
    unknown = sorted(set(doc) - _DOC_FIELDS)
    if unknown:
        raise ValueError(f"unknown top-level field(s) {unknown}")
    if doc.get("schema_version") != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {doc.get('schema_version')!r}; "
            f"this build reads {_SUPPORTED_SCHEMA_VERSION}"
        )
    raw_rates = doc.get("rates")
    if not isinstance(raw_rates, dict) or not raw_rates:
        raise ValueError("'rates' must be a non-empty object")
    out: dict[str, dict[str, Decimal]] = {}
    for endpoint_key, raw in raw_rates.items():
        if not isinstance(endpoint_key, str) or not endpoint_key:
            raise ValueError(f"endpoint key {endpoint_key!r} must be a non-empty string")
        if not isinstance(raw, dict):
            raise ValueError(f"rates[{endpoint_key!r}] must be an object")
        unknown = sorted(set(raw) - _ENTRY_FIELDS)
        if unknown:
            raise ValueError(f"rates[{endpoint_key!r}] has unknown field(s) {unknown}")
        parsed: dict[str, Decimal] = {}
        for field in ("input_per_mtok_usd", "output_per_mtok_usd"):
            if field not in raw:
                raise ValueError(f"rates[{endpoint_key!r}] is missing {field!r}")
            try:
                value = Decimal(str(raw[field]))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"rates[{endpoint_key!r}].{field} is not a number") from exc
            if value < 0:
                raise ValueError(f"rates[{endpoint_key!r}].{field} must not be negative")
            parsed[field] = value
        out[endpoint_key] = parsed
    return out


def _card_for(declared: dict[str, Decimal]) -> Card:
    """A card that answers for any region and either scope.

    Self-hosted capacity has no region-differentiated list price and no
    global-versus-geo distinction, so the same number answers whatever the selector
    asks for. Cache classes are published as an explicit zero rather than omitted:
    omitting them would make `dimensions.select` refuse the model (its completeness rule),
    and the zero here is a fact about vLLM, not an absence of data.
    """
    card: Card = {}
    for scope in SCOPES:
        card[(None, RateDimension("input", scope))] = declared["input_per_mtok_usd"]
        card[(None, RateDimension("output", scope))] = declared["output_per_mtok_usd"]
        card[(None, RateDimension("cache_read", scope))] = Decimal(0)
        card[(None, RateDimension("cache_write", scope))] = Decimal(0)
    return card
