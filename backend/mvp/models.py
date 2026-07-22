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

import os
from dataclasses import dataclass
from typing import Literal, Optional


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
    `bedrock_region` is the per-model AWS region — Claude family is in
    us-east-1; OpenAI family lives in bedrock-mantle in us-east-2/us-west-2.
    `wire_protocol` selects the route handler: `messages` → `mvp.anthropic`,
    `responses` → `mvp.openai_responses`.
    """

    provider: Literal["anthropic", "openai"]
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


# Source of truth. To add a model: append an entry, redeploy. There is no
# runtime override; the registry is intentionally code-resident so reviewers
# can audit every reachable model in the diff.
_REGISTRY: tuple[ModelEntry, ...] = (
    # ---- Anthropic / Claude family (us-east-1) ----
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-opus-4-7",
        bedrock_region="us-east-1",
        aliases=("claude-opus-4-7",),
        wire_protocol="messages",
        pricing_key="opus",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-opus-4-6-v1",
        bedrock_region="us-east-1",
        aliases=("claude-opus-4-6",),
        wire_protocol="messages",
        pricing_key="opus",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-opus-4-5-20251101-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-opus-4-5", "claude-opus-4-5-20251101"),
        wire_protocol="messages",
        pricing_key="opus",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-opus-4-1-20250805-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-opus-4-1", "claude-opus-4-1-20250805"),
        wire_protocol="messages",
        pricing_key="opus",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-opus-4-20250514-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-opus-4", "claude-opus-4-20250514"),
        wire_protocol="messages",
        pricing_key="opus",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-sonnet-4-6",
        bedrock_region="us-east-1",
        aliases=("claude-sonnet-4-6",),
        wire_protocol="messages",
        pricing_key="sonnet",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        wire_protocol="messages",
        pricing_key="sonnet",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
        wire_protocol="messages",
        pricing_key="haiku",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-3-7-sonnet", "claude-3-7-sonnet-20250219"),
        wire_protocol="messages",
        pricing_key="sonnet",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-3-5-haiku-20241022-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-3-5-haiku", "claude-3-5-haiku-20241022"),
        wire_protocol="messages",
        pricing_key="haiku",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-3-haiku-20240307-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-3-haiku",),
        wire_protocol="messages",
        pricing_key="haiku",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-3-opus-20240229-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-3-opus",),
        wire_protocol="messages",
        pricing_key="opus",
    ),
    ModelEntry(
        provider="anthropic",
        bedrock_model_id="us.anthropic.claude-3-sonnet-20240229-v1:0",
        bedrock_region="us-east-1",
        aliases=("claude-3-sonnet",),
        wire_protocol="messages",
        pricing_key="sonnet",
    ),
    # ---- OpenAI family on Bedrock (bedrock-mantle, us-east-2 / us-west-2) ----
    # GPT-5.4 is GA in us-east-2 and us-west-2; verified working in us-west-2
    # against the existing operator's codex config. GPT-5.5 is currently
    # us-east-2 only.
    ModelEntry(
        provider="openai",
        bedrock_model_id="openai.gpt-5.4",
        bedrock_region="us-west-2",
        aliases=("gpt-5.4", "openai.gpt-5.4"),
        wire_protocol="responses",
        pricing_key="gpt-5",
    ),
    ModelEntry(
        provider="openai",
        bedrock_model_id="openai.gpt-5.5",
        bedrock_region="us-east-2",
        aliases=("gpt-5.5", "openai.gpt-5.5"),
        wire_protocol="responses",
        pricing_key="gpt-5",
    ),
)


_ALIAS_MAP: dict[str, ModelEntry] = {
    alias: entry for entry in _REGISTRY for alias in entry.aliases
}
# Bedrock IDs are themselves valid client-facing identifiers (clients that
# already speak Bedrock-native names). Allow them to round-trip through
# resolve_model() but only for entries that exist in the registry.
_BEDROCK_ID_MAP: dict[str, ModelEntry] = {
    entry.bedrock_model_id: entry for entry in _REGISTRY
}


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


def assert_vllm_cache_rates_zero() -> None:
    """Assert every vLLM entry's pricing key has zero cache read/write rates.
    Called lazily (e.g. at first hybrid use / in tests) rather than at import
    to avoid a models<->pricing import cycle."""
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


_validate_registry(_REGISTRY)


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
            f"model '{name}' is not in the allowlist. "
            "Supported models are listed in backend/mvp/models.py:_REGISTRY."
        )
    return entry


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

    raise ValueError(
        f"model '{anthropic_model}' is not allowed. "
        f"Only Claude family models are supported by the Anthropic Messages route."
    )
