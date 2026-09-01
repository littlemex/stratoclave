"""Model registry for the Bedrock proxy.

Maps client-facing model identifiers to Bedrock model IDs and metadata
required to route a request: which provider (anthropic / openai), which
Bedrock region, and which wire protocol the route handler should speak.

The legacy `_MAPPING` dict and `resolve_bedrock_model()` shim are preserved
for backward compatibility with `mvp.anthropic` and any external imports
during the migration window. New code should call `resolve_model()` and
read `ModelEntry` fields directly.

The set of allowed models is enumerated explicitly: any client-supplied
model ID outside this list is rejected with HTTP 400 by the route layer
before credit reservation. Unsupported models would otherwise reach
Bedrock with no token-accounting policy attached.
"""
from __future__ import annotations

import difflib
import json
import os

from .rates import no_duplicate_keys as _no_duplicate_keys
from dataclasses import dataclass
from typing import Literal, NoReturn, Optional


# MVP default for the Anthropic Messages route. OpenAI route uses its own
# default sourced from `DEFAULT_CODEX_MODEL` env, resolved at the CLI/CDK layer.
DEFAULT_MODEL = os.getenv(
    "DEFAULT_BEDROCK_MODEL",
    "us.anthropic.claude-opus-4-7",
)


@dataclass(frozen=True)
class ModelEntry:
    """A single allowed model entry.

    `aliases` is the set of client-facing identifiers (Anthropic SDK names,
    short codex-style names, raw Bedrock IDs) that map to this entry.
    `bedrock_region` is asymmetric on purpose. For `wire_protocol="responses"` it
    is authoritative: each model on the OpenAI-compatible endpoint is offered in specific regions
    (us-east-2 / us-west-2). For `"messages"` it is advisory — the Converse chain
    is built from the deployment's region policy (`routing.chains`), so a Converse
    model must be offered there or it should not be registered.
    `wire_protocol` selects the route handler: `messages` → `mvp.anthropic`,
    `responses` → `mvp.openai_responses`.
    """

    provider: Literal["anthropic", "openai", "xai", "google", "nvidia", "qwen"]
    bedrock_model_id: str
    bedrock_region: str
    aliases: tuple[str, ...]
    wire_protocol: Literal["messages", "responses"]
    # `pricing_key` names the row in the pricing table (and the built-in
    # default rate table in `mvp.pricing`) used to convert this model's token
    # counts into micro-USD for dollar-denominated budgets. Models that share
    # a price tier share a key (e.g. all Opus 4.x → "opus"). It is decoupled
    # from `bedrock_model_id` so that re-pricing a tier does not require
    # touching every registry entry.
    pricing_key: str = "default"
    # Hybrid serving (P0). "bedrock" (default) == today. "vllm" means this
    # model is served by a self-hosted, internal OpenAI-compatible vLLM
    # endpoint keyed by `endpoint_key` (resolved against an operator allowlist,
    # never a URL). A vLLM entry is only servable when HYBRID_SERVING_ENABLED
    # is on AND the key is in the allowlist; its pricing fields are an
    # operator-set micro-USD cost-recovery rate, and its cache rates MUST be 0
    # (vLLM reports no Bedrock cache-token split). Enforced at registry load.
    served_by: Literal["bedrock", "vllm", "semantic-router"] = "bedrock"
    endpoint_key: Optional[str] = None
    # The id the PRICE APIs know this model by, when it differs from the id the
    # gateway invokes. Both price sources key on the provider's own id — the
    # agreement API rejects an inference-profile-prefixed id outright, and the Price
    # List embeds the billed id in its usage types — and the billed spelling is not
    # always derivable: `qwen.qwen3-next-80b-a3b` is billed as `...-a3b-instruct`.
    # Declared rather than inferred, because the alternative is a prefix match that
    # cannot tell a variant of the same model from a different, dearer one (`xai.grok-4`
    # would swallow every `xai.grok-4.6` row). Absent means "strip the inference-profile
    # prefix and use that", which is right for every current entry but one.
    price_model_id: Optional[str] = None
    # SR integration (option B). A "semantic-router" entry is a VIRTUAL pool
    # entry: it names the SR pool (`sr_pool_ref`) rather than a concrete model,
    # and it is used ONLY as a candidate-chain / reservation entry point. It is
    # NEVER a charge-of-record model — at settle the real model that SR executed
    # is normalized from the router-replay evidence and charged at the ledger's
    # snapshot price. `virtual=True` marks entries that must never appear as the
    # billed model. No registry entry uses these yet (SR ships dark); they are
    # the seam types the SR adapter fills in a later substep.
    virtual: bool = False
    sr_pool_ref: Optional[str] = None


# Source of truth: an external JSON document, not a Python literal. Operators add
# models by editing data and redeploying — no code change, no Python syntax to get
# wrong, and the file is reviewable as a table. The default lives next to this
# module; STRATOCLAVE_MODEL_REGISTRY_PATH points at a different one.
#
# The registry is still fully validated at import: a malformed or self-inconsistent
# document raises here rather than surfacing as a mysterious 400 (or, worse, a
# request routed to a model with no accounting policy) on the hot path.
_REGISTRY_FILENAME = "defaults/models.json"
_SUPPORTED_SCHEMA_VERSION = 1

_PROVIDERS = frozenset({"anthropic", "openai", "xai", "google", "nvidia", "qwen"})
_WIRE_PROTOCOLS = frozenset({"messages", "responses"})
_SERVED_BY = frozenset({"bedrock", "vllm", "semantic-router"})
# `notes` is documentation carried in the data; it has no runtime effect.
_ENTRY_FIELDS = frozenset({
    "provider", "bedrock_model_id", "bedrock_region", "aliases", "wire_protocol",
    "pricing_key", "served_by", "endpoint_key", "virtual", "sr_pool_ref", "notes",
    "price_model_id",
})
# `pricing_key` is required rather than defaulted. Defaulting a typo to "default"
# charges the model at the `default` rate, and `default` is NOT an upper bound — the
# fable tier is priced above it — so a mistyped key can under-charge.
# `$comment` is the document's own prose; the rest is structure.
_DOC_FIELDS = frozenset({"schema_version", "models", "$comment"})
_REQUIRED_FIELDS = ("provider", "bedrock_model_id", "bedrock_region", "aliases",
                    "wire_protocol", "pricing_key")
_STRING_FIELDS = ("provider", "bedrock_model_id", "bedrock_region", "wire_protocol",
                  "pricing_key", "endpoint_key", "sr_pool_ref", "notes",
                  "price_model_id")
# Regions where the OpenAI-compatible surface serves these models. `bedrock_region` is AUTHORITATIVE
# for a responses entry — that is where the prompt goes — so a typo'd region must not
# reach the transport, which would fail with a confusing connection error at best.
#
# Deliberately a STATIC fact about the service, not a deployment setting. In
# particular it is NOT read from OPENAI_BEDROCK_REGIONS: that variable is a
# display-only hint, and `tests/test_openai_region_residency_contract.py` pins that
# it must never move a registry region, because the IaC residency analysis
# (`iac/lib/region-config.ts`) reads the registry and ignores the variable. Making
# the variable load-bearing here would silently invalidate that analysis. Residency
# policy is enforced there; this check only rejects a region the endpoint does not have.
# Per the model cards: the OpenAI and xAI families are offered in us-east-2/us-west-2,
# and Gemma 4 adds us-east-1 and eu-central-1. Union of the two, because this check
# only rejects a region the endpoint does not serve at all — which model is offered where is
# the entry author's business.
_OPENAI_ENDPOINT_REGIONS = frozenset({"us-east-1", "us-east-2", "us-west-2", "eu-central-1"})


def _validate_registry(registry: tuple[ModelEntry, ...]) -> None:
    """Fail fast at import time on an incoherent registry. Currently enforces
    the hybrid-serving (vLLM) invariants so a mis-authored vLLM entry cannot
    ship: a vLLM entry MUST name an `endpoint_key` (the opaque allowlist token
    — never a URL) and MUST price its cache tokens at 0 (vLLM reports no
    Bedrock cache-token split, so any nonzero cache rate would be dead pricing
    that also biases SAAR's warm-prefix delta). Cache rates are validated
    lazily against the pricing module to avoid an import cycle at module load."""
    for entry in registry:
        if getattr(entry, "served_by", "bedrock") != "vllm":
            continue
        if not entry.endpoint_key:
            raise ValueError(
                f"vLLM model entry '{entry.bedrock_model_id}' must set endpoint_key"
            )


def registry_path() -> str:
    """Path of the registry document actually in effect."""
    override = os.getenv("STRATOCLAVE_MODEL_REGISTRY_PATH")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _REGISTRY_FILENAME)


def _fail(path: str, message: str) -> NoReturn:
    raise ValueError(f"model registry {path}: {message}")


def _parse_entry(path: str, index: int, raw: object) -> ModelEntry:
    where = f"models[{index}]"
    if not isinstance(raw, dict):
        _fail(path, f"{where} must be an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _ENTRY_FIELDS)
    if unknown:
        # Loud rather than ignored: a typo'd key is otherwise a silently dropped
        # setting (e.g. "region" instead of "bedrock_region").
        _fail(path, f"{where} has unknown field(s) {unknown}; allowed: {sorted(_ENTRY_FIELDS)}")
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            _fail(path, f"{where} is missing required field {field!r}")
        if raw[field] in (None, "", [], {}):
            _fail(path, f"{where} has an empty required field {field!r}")
    for field in _STRING_FIELDS:
        value = raw.get(field)
        # Truthiness is not a type check: `"bedrock_region": 123` would otherwise
        # pass here and fail much later inside boto3.
        if value is not None and not isinstance(value, str):
            _fail(path, f"{where}.{field} must be a string, got {type(value).__name__}")
    virtual = raw.get("virtual", False)
    # `bool(...)` would read the string "false" as True, and `virtual` is the flag
    # that keeps an entry from ever being a charge-of-record model.
    if not isinstance(virtual, bool):
        _fail(path, f"{where}.virtual must be a JSON boolean, got {virtual!r}")
    if raw["provider"] not in _PROVIDERS:
        _fail(path, f"{where}.provider {raw['provider']!r} not in {sorted(_PROVIDERS)}")
    if raw["wire_protocol"] not in _WIRE_PROTOCOLS:
        _fail(path, f"{where}.wire_protocol {raw['wire_protocol']!r} not in {sorted(_WIRE_PROTOCOLS)}")
    if raw["wire_protocol"] == "responses" and raw.get("served_by", "bedrock") == "bedrock":
        if raw["bedrock_region"] not in _OPENAI_ENDPOINT_REGIONS:
            _fail(path, f"{where}.bedrock_region {raw['bedrock_region']!r} is not a region where "
                        f"the OpenAI-compatible endpoint serves {sorted(_OPENAI_ENDPOINT_REGIONS)}; a responses entry's "
                        f"region is authoritative — that is where the prompt goes")
    served_by = raw.get("served_by", "bedrock")
    if served_by not in _SERVED_BY:
        _fail(path, f"{where}.served_by {served_by!r} not in {sorted(_SERVED_BY)}")
    aliases = raw["aliases"]
    if not isinstance(aliases, list) or not all(isinstance(a, str) and a for a in aliases):
        _fail(path, f"{where}.aliases must be a non-empty list of non-empty strings")
    if len(set(aliases)) != len(aliases):
        _fail(path, f"{where}.aliases repeats an alias: {aliases}")
    # A virtual entry stands for a semantic-router pool rather than a model. Without
    # both of these it would look like an ordinary billable entry, and `virtual` is
    # the only thing keeping it from becoming a charge of record.
    if virtual:
        if served_by != "semantic-router":
            _fail(path, f"{where} is virtual, so served_by must be 'semantic-router', got {served_by!r}")
        if not raw.get("sr_pool_ref"):
            _fail(path, f"{where} is virtual, so it must name the pool it stands for in sr_pool_ref")
    elif raw.get("sr_pool_ref"):
        _fail(path, f"{where} sets sr_pool_ref but is not virtual")
    # An id whose first segment looks like an inference-profile prefix this build does not
    # know cannot be priced: the price APIs reject a prefixed id, and stripping an unknown
    # prefix on a guess mangles a bare id whose second dot is a version number
    # (`xai.grok-4.6`). Either outcome ends with the model on the bundled floor for as long
    # as nobody notices, so the entry has to say which id the price APIs know it by.
    if served_by == "bedrock" and not raw.get("price_model_id"):
        from .pricing_feeds.dimensions import unknown_profile_prefix

        unknown = unknown_profile_prefix(raw["bedrock_model_id"])
        if unknown:
            _fail(path, f"{where}.bedrock_model_id starts with {unknown!r}, which is not a "
                        f"known inference-profile prefix. Set price_model_id to the id the "
                        f"price APIs know this model by; guessing which segment to strip "
                        f"silently prices the model at the bundled floor")
    # Checked here as well as in `_validate_registry` so the message names the file
    # and the entry index; a self-hosted entry without an endpoint key would otherwise be
    # routed as if it were Bedrock.
    if served_by == "vllm" and not raw.get("endpoint_key"):
        _fail(path, f"{where} is served_by 'vllm', so it must name an endpoint_key "
                    f"(an operator allowlist token, never a URL)")
    return ModelEntry(
        provider=raw["provider"],
        bedrock_model_id=raw["bedrock_model_id"],
        bedrock_region=raw["bedrock_region"],
        aliases=tuple(aliases),
        wire_protocol=raw["wire_protocol"],
        pricing_key=raw["pricing_key"],
        price_model_id=raw.get("price_model_id"),
        served_by=served_by,
        endpoint_key=raw.get("endpoint_key"),
        virtual=virtual,
        sr_pool_ref=raw.get("sr_pool_ref"),
    )


def load_registry(path: Optional[str] = None) -> tuple[ModelEntry, ...]:
    """Read, validate and freeze the registry document at `path`.

    Raises `ValueError` on anything self-inconsistent. Separate from module import
    so a test (or an operator's pre-deploy check) can validate a candidate file
    without swapping the process-wide registry.
    """
    path = path or registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh, object_pairs_hook=_no_duplicate_keys)
    except FileNotFoundError:
        _fail(path, "file not found")
    except OSError as exc:
        _fail(path, f"cannot read: {exc}")
    except json.JSONDecodeError as exc:
        _fail(path, f"invalid JSON: {exc}")
    if not isinstance(doc, dict):
        _fail(path, "top level must be an object")
    unknown_top = sorted(set(doc) - _DOC_FIELDS)
    if unknown_top:
        _fail(path, f"unknown top-level field(s) {unknown_top}; allowed: {sorted(_DOC_FIELDS)}")
    version = doc.get("schema_version")
    if version != _SUPPORTED_SCHEMA_VERSION:
        # Refuse an unknown schema instead of guessing which fields still mean
        # what they used to.
        _fail(path, f"unsupported schema_version {version!r}; this build reads {_SUPPORTED_SCHEMA_VERSION}")
    models = doc.get("models")
    if not isinstance(models, list) or not models:
        _fail(path, "\"models\" must be a non-empty array")

    entries = tuple(_parse_entry(path, i, raw) for i, raw in enumerate(models))

    # Uniqueness across the WHOLE namespace a client can address: aliases and
    # Bedrock ids are both resolvable, so a collision between them is as
    # ambiguous as a duplicate alias.
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        # Tracked by entry INDEX, not by model id: two entries sharing one
        # bedrock_model_id (with different pricing keys, say) used to pass because
        # the id matched itself, leaving `_BEDROCK_ID_MAP` last-writer-wins and the
        # charged rate dependent on document order.
        for name in (*entry.aliases, entry.bedrock_model_id):
            previous = seen.get(name)
            if previous is not None and previous != index:
                _fail(path, f"name {name!r} is claimed by models[{previous}] "
                            f"({entries[previous].bedrock_model_id!r}) and models[{index}] "
                            f"({entry.bedrock_model_id!r})")
            seen[name] = index

    # Every pricing_key must name a real rate row in the BUNDLED document. A key that
    # only a live source supplies is deliberately not enough: the floor is the layer
    # guaranteed to load, so a model whose key exists nowhere else would be charged at
    # `default` the moment the feed is unavailable. Add the row to the bundled
    # document (a conservative rate is fine — the source raises it).
    from .price_sources import load_rate_document

    try:
        known_keys = set(load_rate_document())
    except Exception as exc:  # noqa: BLE001 — surface as a registry error, with cause.
        _fail(path, f"cannot validate pricing keys: {exc}")
    for index, entry in enumerate(entries):
        if entry.pricing_key not in known_keys:
            _fail(path, f"models[{index}] pricing_key {entry.pricing_key!r} has no rate row; "
                        f"known keys: {sorted(known_keys)}")
    _validate_registry(entries)
    return entries


# Loaded and validated once at import. A bad document fails the process start
# rather than the first request that happens to touch it.
_REGISTRY: tuple[ModelEntry, ...] = load_registry()


_ALIAS_MAP: dict[str, ModelEntry] = {
    alias: entry for entry in _REGISTRY for alias in entry.aliases
}
# Bedrock IDs are themselves valid client-facing identifiers (clients that
# already speak Bedrock-native names). Allow them to round-trip through
# resolve_model() but only for entries that exist in the registry.
_BEDROCK_ID_MAP: dict[str, ModelEntry] = {
    entry.bedrock_model_id: entry for entry in _REGISTRY
}


def assert_vllm_cache_rates_zero() -> None:
    """Assert every vLLM entry's pricing key has zero cache read/write rates.

    Checked against the EFFECTIVE table (floor + active source + admin overrides),
    not the bundled floor: a price source or an override could reintroduce a nonzero
    cache rate that the floor does not have, and the invariant is about what actually
    gets charged. Called lazily (first hybrid use / tests) rather than at import to
    avoid a models<->pricing import cycle."""
    from .pricing import _cache

    for entry in _REGISTRY:
        if getattr(entry, "served_by", "bedrock") != "vllm":
            continue
        rate = _cache.get(entry.pricing_key)
        if rate.cache_read_per_mtok_microusd != 0 or rate.cache_write_per_mtok_microusd != 0:
            raise ValueError(
                f"vLLM entry '{entry.bedrock_model_id}' (pricing_key="
                f"'{entry.pricing_key}') must have zero cache rates"
            )




# A suggestion is only useful if acting on it succeeds. Containment matching a
# very short alias would fire on almost any input ("sol" inside anything), so
# containment is gated on this length; shorter aliases are still reachable
# through the lexical pass below.
_MIN_CONTAINED_ALIAS = 8
# The echoed name is bounded: it is attacker-supplied, it is scanned against
# every candidate, and it ends up in a response body.
_MAX_ECHOED_NAME = 120


def _did_you_mean(name: Optional[str], *, limit: int = 3,
                  only: Optional[frozenset[str]] = None) -> str:
    """A ``did you mean`` fragment built from the live alias map.

    Derived from the registry rather than hand-listed, so it cannot drift when
    models are added or renamed. Returns "" when nothing is close enough, so the
    caller can concatenate it unconditionally.

    Only aliases are ever suggested — never raw Bedrock ids. The sentence that
    follows a suggestion points at ``GET /v1/models``, and that endpoint lists
    aliases, so suggesting anything else would contradict it.

    ``only`` restricts the candidate set to names the calling route can actually
    serve. Without it the Anthropic Messages route would suggest an OpenAI model
    and then, in the next sentence, refuse it — a suggestion that cannot be
    followed is worse than none.
    """
    if not name:
        return ""
    name = name[:_MAX_ECHOED_NAME]
    lowered = name.casefold()
    candidates = sorted(_ALIAS_MAP if only is None else (a for a in _ALIAS_MAP if a in only))
    # Containment first, and deliberately so. A caller who sent something close
    # to a full Bedrock id ("us.anthropic.claude-haiku-4-5") wants the short
    # alias it contains ("claude-haiku-4-5"); pure lexical distance would answer
    # with a different model of a similar name ("claude-opus-5"), which is a
    # worse answer than no answer. Longest first: the most specific contained
    # alias is the one meant. Case-folded, because a wrong case is the cheapest
    # mistake to make and the lexical pass scores it far below its cutoff.
    contained = sorted(
        (a for a in candidates
         if len(a) >= _MIN_CONTAINED_ALIAS and a.casefold() in lowered),
        key=len, reverse=True,
    )
    close = contained[:limit]
    if not close:
        close = difflib.get_close_matches(lowered, candidates, n=limit, cutoff=0.6)
    if not close:
        return ""
    return " Did you mean " + " or ".join(f"'{c}'" for c in close) + "?"


def resolve_model(name: Optional[str]) -> ModelEntry:
    """Resolve a client-facing model name to a `ModelEntry`.

    Falls back to `DEFAULT_MODEL` when `name` is empty/None. Raises
    `ValueError` for any name not in the allowlist; the route layer maps
    that to HTTP 400.
    """
    if not name:
        name = DEFAULT_MODEL
    entry = _ALIAS_MAP.get(name) or _BEDROCK_ID_MAP.get(name)
    if entry is None:
        raise ValueError(
            f"model '{name[:_MAX_ECHOED_NAME]}' is not in the allowlist."
            f"{_did_you_mean(name)} "
            "The full list of accepted model names is served by GET /v1/models."
        )
    return entry


def canonical_model_id(name: str) -> str:
    """The one spelling of a model that every layer keys on: its registry primary
    alias (the Bedrock id when an entry declares no alias).

    A model reaches this system under several names — a short alias, a dated
    alias, an inference-profile-prefixed Bedrock id — and each of them names the
    SAME entry, hence the same price and the same quota. Anything that identifies
    a model by the string the caller happened to send therefore holds as many
    identities as there are spellings, and a control keyed that way is bypassed by
    respelling its subject. That is why the routing config is stored canonicalised
    on write and why the per-model quota counter is keyed here.

    An unresolvable name maps to ITSELF rather than raising: callers use this to
    compare and to key, so a name outside the registry must simply fail to match
    anything instead of turning a lookup into an error. An EMPTY name is likewise
    returned unchanged — `resolve_model` reads it as "give me the default model",
    which is the right answer when serving a request and the wrong one when
    identifying which model a counter or a config entry is about.
    """
    if not name:
        return name
    try:
        entry = resolve_model(name)
    except ValueError:
        return name
    return entry.aliases[0] if entry.aliases else entry.bedrock_model_id


def registry_entries() -> tuple[ModelEntry, ...]:
    """Read-only view of the model registry (the code-resident allowlist). Used by
    the shadow VSR to find the cheapest model in a price tier; a plain accessor so
    callers never import the private `_REGISTRY`."""
    return _REGISTRY


# ---------------------------------------------------------------------------
# Backward-compatibility shims
# ---------------------------------------------------------------------------
# `mvp.anthropic` (line 50) imports `_MAPPING` and `resolve_bedrock_model`
# at module top-level. Keep both working unchanged so that the model-registry
# refactor lands as a pure additive change. New code should not import
# `_MAPPING`; use `_REGISTRY` filtered by `provider == "anthropic"` instead.

_MAPPING: dict[str, str] = {
    alias: entry.bedrock_model_id
    for entry in _REGISTRY
    if entry.provider == "anthropic"
    for alias in entry.aliases
}

_ALLOWED_BEDROCK_MODELS: frozenset[str] = frozenset(
    list(_MAPPING.values()) + [DEFAULT_MODEL]
)


# Aliases the Anthropic Messages route can actually serve. Derived from the
# registry so it cannot drift; used to keep suggestions on that route followable.
_MESSAGES_ROUTE_ALIASES: frozenset[str] = frozenset(
    a for a, e in _ALIAS_MAP.items()
    if (_MAPPING.get(a) is not None) or (e.bedrock_model_id in _ALLOWED_BEDROCK_MODELS)
)


def resolve_bedrock_model(anthropic_model: Optional[str]) -> str:
    """Legacy resolver: returns the Bedrock model ID for an Anthropic name.

    Restricted to the Anthropic subset of the registry to preserve the
    previous "Claude-only" guarantee for callers (e.g. `mvp.anthropic`).
    OpenAI models route through `mvp.openai_responses` and resolve through
    `resolve_model()` directly.
    """
    if not anthropic_model:
        return DEFAULT_MODEL

    mapped = _MAPPING.get(anthropic_model)
    if mapped is not None:
        return mapped

    if anthropic_model in _ALLOWED_BEDROCK_MODELS:
        return anthropic_model

    # Distinguish "known model, wrong route" from "unknown model". The old
    # message said "Only Claude family models are supported" for BOTH, which
    # reads as a contradiction when the rejected name IS a Claude model whose
    # only problem is that it is not a registered alias (found in live
    # verification, 2026-08-27).
    known = _ALIAS_MAP.get(anthropic_model) or _BEDROCK_ID_MAP.get(anthropic_model)
    shown = anthropic_model[:_MAX_ECHOED_NAME]
    if known is not None:
        raise ValueError(
            f"model '{shown}' is not served by the Anthropic Messages "
            f"route. Use /v1/chat/completions or /openai/v1/responses for "
            f"provider '{known.provider}' models."
        )
    raise ValueError(
        f"model '{shown}' is not a recognised model name."
        f"{_did_you_mean(anthropic_model, only=_MESSAGES_ROUTE_ALIASES)} "
        "The Anthropic Messages route accepts Claude family names; the full list "
        "of accepted names is served by GET /v1/models."
    )
