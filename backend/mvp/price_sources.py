"""Where baseline prices come from.

Effective pricing is resolved in three layers, cheapest-to-change last:

1. **the bundled JSON** (`defaults/pricing.json`) — the floor. Loaded from disk with
   no network and no credentials, so it cannot fail at runtime. Every other layer
   degrades to this one.
2. **the active price source** — this module. `STRATOCLAVE_PRICE_SOURCE` names a
   registered source; the default (`json`) is layer 1 read through the same
   interface. A deployment that wants real prices registers a source that fetches
   them (AWS Price List API, an internal rate service, a nightly export) without
   touching the charging code.
3. **admin overrides** in the PricingConfig table — highest precedence, applied by
   `mvp.pricing` on top of whatever this module returned.

A source returns a whole table, not one rate at a time: a partially-refreshed
table could charge one request at old input rates and new output rates. It is
called on the pricing cache's refresh interval, never on the request path, and a
raising source is fail-static — the previous good table stays in force.

Registering a live source:

    from mvp.price_sources import PriceSource, register_price_source

    class MyRateService:
        name = "my-service"
        def load(self):
            return {"opus": Rate(...), ...}   # micro-USD per MTok

    register_price_source(MyRateService())

then deploy with `STRATOCLAVE_PRICE_SOURCE=my-service`. Registration must happen
at import time of a module the app loads; an unknown name is a hard failure at
first use rather than a silent fallback, because silently charging the bundled
floor when an operator asked for live prices is a billing error, not a warning.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional, Protocol

from .rates import no_duplicate_keys as _no_duplicate_keys, RATE_FIELDS, Rate, validate_rate_table

from core.logging import get_logger

logger = get_logger(__name__)

_PRICING_FILENAME = "defaults/pricing.json"
_SUPPORTED_SCHEMA_VERSION = 1
# `notes` carries rationale in the data, like the model registry's. Anything else is
# a typo, and a typo'd key is a silently dropped setting — the same reason the model
# registry rejects unknown fields rather than ignoring them.
_ALLOWED_RATE_KEYS = frozenset(RATE_FIELDS) | {"notes"}
# `$comment` is the document's own prose; the rest is structure.
_DOC_FIELDS = frozenset({"schema_version", "rates", "$comment"})
DEFAULT_SOURCE_NAME = "json"
# Every layer degrades to this key, so a table without it cannot price an unknown
# model at all.
REQUIRED_KEY = "default"


class PriceSourceConfigError(ValueError):
    """The active price source is misconfigured — an unknown name, or a source that
    does not satisfy the interface.

    Its own type so the pricing cache can let it out instead of absorbing it as a
    transient. A configuration error does not get better by retrying, and charging
    the bundled floor because someone typo'd an env var is a billing error.
    """


def pricing_path() -> str:
    """Path of the bundled rate document actually in effect."""
    override = os.getenv("STRATOCLAVE_PRICING_PATH")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _PRICING_FILENAME)


def load_rate_document(path: Optional[str] = None) -> Dict[str, Rate]:
    """Read and validate the bundled rate document into `{key: Rate}`."""
    path = path or pricing_path()

    def fail(message: str):
        raise ValueError(f"pricing document {path}: {message}")

    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh, object_pairs_hook=_no_duplicate_keys)
    except FileNotFoundError:
        fail("file not found")
    except OSError as exc:
        # A directory, a permission problem, an unreadable mount: still a bad
        # document, and a raw traceback would not say which file.
        fail(f"cannot read: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON: {exc}")
    if not isinstance(doc, dict):
        fail("top level must be an object")
    if doc.get("schema_version") != _SUPPORTED_SCHEMA_VERSION:
        fail(
            f"unsupported schema_version {doc.get('schema_version')!r}; "
            f"this build reads {_SUPPORTED_SCHEMA_VERSION}"
        )
    unknown_top = sorted(set(doc) - _DOC_FIELDS)
    if unknown_top:
        fail(f"unknown top-level field(s) {unknown_top}; allowed: {sorted(_DOC_FIELDS)}")
    raw_rates = doc.get("rates")
    if not isinstance(raw_rates, dict) or not raw_rates:
        fail('"rates" must be a non-empty object')

    rates: Dict[str, Rate] = {}
    for key, raw in raw_rates.items():
        if not isinstance(raw, dict):
            fail(f"rates[{key!r}] must be an object")
        unknown = sorted(set(raw) - _ALLOWED_RATE_KEYS)
        if unknown:
            fail(f"rates[{key!r}] has unknown field(s) {unknown}; allowed: {sorted(_ALLOWED_RATE_KEYS)}")
        values = {}
        for field in RATE_FIELDS:
            value = raw.get(field)
            # Reject a missing or non-integer field rather than defaulting it to
            # zero: a zero rate silently stops charging for that token class.
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                fail(f"rates[{key!r}].{field} must be a non-negative integer, got {value!r}")
            values[field] = value
        rates[key] = Rate(**values)
    if REQUIRED_KEY not in rates:
        fail(f'rates must define the {REQUIRED_KEY!r} key (the fallback for an unpriced model)')
    return rates


class JsonPriceSource:
    """The bundled document, read through the source interface.

    Read once and memoised, deliberately. Re-reading per refresh would make
    filesystem write access inside the container a way to change prices with no
    deploy and no review — a wider trust boundary than the model registry has, which
    is read once at import. Repricing without a deploy is what the admin override
    layer is for, and that path is audited.
    """

    name = DEFAULT_SOURCE_NAME

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._table: Optional[Dict[str, Rate]] = None

    def load(self) -> Dict[str, Rate]:
        if self._table is None:
            self._table = load_rate_document(self._path)
        return self._table


class PriceSource(Protocol):
    """What a price source has to look like.

    Structural, not a base class: a deployment can register a plain object (or a
    module-level singleton) without importing anything from here, which keeps a
    rate-service client free of a dependency on the gateway's internals.
    """

    name: str

    def load(self) -> Dict[str, Rate]:
        """The whole rate table, keyed by pricing key. Raising is allowed and
        handled: the caller keeps the last good table."""
        ...


_SOURCES: Dict[str, PriceSource] = {}


def register_price_source(source: PriceSource, *, replace: bool = False) -> None:
    """Register a price source under its `name`.

    Re-registering the same name is an error unless `replace=True`. Two modules
    silently fighting over one name would make the effective rate table depend on
    import order.
    """
    name = getattr(source, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("a price source must expose a non-empty string `name`")
    if not callable(getattr(source, "load", None)):
        raise ValueError(f"price source {name!r} must expose a callable `load()`")
    if name == DEFAULT_SOURCE_NAME and replace:
        # The bundled name is what the provenance label reads as "built-in". Letting a
        # live feed take that name would stamp feed prices as `builtin` on the ledger,
        # which is exactly the dispute ambiguity the sentinels exist to prevent.
        raise PriceSourceConfigError(
            f"the built-in source name {DEFAULT_SOURCE_NAME!r} may not be replaced; "
            "register a live source under its own name and select it with "
            "STRATOCLAVE_PRICE_SOURCE"
        )
    if name in _SOURCES and not replace:
        raise ValueError(f"price source {name!r} is already registered")
    _SOURCES[name] = source


def registered_source_names() -> tuple[str, ...]:
    return tuple(sorted(_SOURCES))


def active_source_name() -> str:
    return os.getenv("STRATOCLAVE_PRICE_SOURCE") or DEFAULT_SOURCE_NAME


def active_source() -> PriceSource:
    """The source named by `STRATOCLAVE_PRICE_SOURCE`.

    An unknown name raises. Falling back to the bundled floor would charge the
    wrong prices for as long as nobody reads the logs, and pricing is the one place
    where a quiet default is worse than a crash.
    """
    name = active_source_name()
    source = _SOURCES.get(name)
    if source is None:
        raise PriceSourceConfigError(
            f"unknown price source {name!r}; registered: {list(registered_source_names())}. "
            "Register it at import time or unset STRATOCLAVE_PRICE_SOURCE."
        )
    return source


def load_from_active_source() -> Dict[str, Rate]:
    """Resolve the active source, call it, and validate what it returns.

    Resolution and loading are deliberately NOT wrapped together by the caller: an
    unknown source name is a configuration error that must surface, while a load
    failure is a transient the caller rides out. `active_source()` therefore raises
    out of this function, and only `load()` failures are the caller's to absorb.
    """
    source = active_source()
    return validate_rate_table(source.load(), origin=f"price source {source.name!r}")


def validate_configuration() -> str:
    """Resolve AND load the active source once, at process start. Returns its name.

    This is where a misconfigured price source is supposed to fail: the request path
    deliberately degrades rather than breaking admission (see `_pipeline`'s reserve
    handler and its `snapshot-failed` sentinel), which is the right call for a
    transient but would mean a typo'd `STRATOCLAVE_PRICE_SOURCE` charges the bundled
    floor for as long as the task stays up. Failing the deployment instead means the
    misconfiguration never reaches live traffic.

    Also refuses a document that does not price every `pricing_key` the model
    registry uses. A pricing-key split (adding `opus-legacy`, `sonnet-5`, `sonnet-3`,
    `haiku-3-5`, `haiku-3` alongside the families they came from) leaves a deployment
    carrying its own older document with no rate row for the new keys, and
    `mvp.pricing` falls back to `default` for anything absent — so the split silently
    charges those families at whatever `default` happens to be, and an admin override
    written against the old key set stops applying to the models it was written for.
    Checked against THIS source's own returned table, not the layered effective table
    `mvp.pricing` computes: this function takes no repository and makes no AWS call by
    design, and an admin override is dynamic — it can be withdrawn at any moment, so a
    document that is complete only while an override is applied is not complete. An
    override supplying a missing key does not rescue startup.
    """
    from .models import registry_entries  # deferred: models <-> price_sources import cycle

    source = active_source()
    table = validate_rate_table(source.load(), origin=f"price source {source.name!r}")
    used_keys = {entry.pricing_key for entry in registry_entries()}
    missing = sorted(used_keys - set(table))
    if missing:
        raise PriceSourceConfigError(
            f"price source {source.name!r} has no rate row for pricing_key(s) "
            f"{missing} that the model registry uses; mvp.pricing would fall back to "
            "'default' for every model on one of these keys"
        )
    return source.name


def reset_registry_for_tests() -> None:
    """Drop registrations and re-register the built-in source."""
    _SOURCES.clear()
    register_price_source(JsonPriceSource())


register_price_source(JsonPriceSource())
