"""SR decision-name → Stratoclave registry model_id mapping (architecture A').

The vLLM Semantic Router returns a `decision_name` from ITS OWN namespace (the
decision names configured in the router's config, e.g. "reasoning", "cheap-chat",
or a model-ish name). Stratoclave must translate that into one of ITS registry
model_ids before the decision can flow through the reserve — a decision it cannot
map is a config gap, and the safe response is NO_DECISION (fail-open), never a
pass-through of an unknown model into the money path.

The map is operator config: `SEMANTIC_ROUTER_DECISION_MAP` is a JSON object
{ "<sr_decision_name>": "<registry_model_id>", ... }. Two safety properties:

  * Identity fallback: if a decision name is ALREADY a known registry model_id,
    it maps to itself (so a router configured to emit registry ids Just Works
    without a map entry). An explicit map entry always wins over identity.
  * CI gate (tests/ci): every VALUE in the configured map must be a registry
    model that is enabled + priced, or the deploy is rejected — the same
    discipline as the SR pool pricing gate. This module exposes the pure
    validation helper the gate calls.

`normalize_decision` is a pure function of (decision_name, map, is_known_model)
so it is trivially testable and identical online and in the CI gate.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from core.logging import get_logger

logger = get_logger(__name__)


def _load_map() -> dict:
    """Parse SEMANTIC_ROUTER_DECISION_MAP (JSON object). Malformed ⇒ empty map +
    a warning (fail-open: with no map, only identity mapping applies)."""
    raw = (os.getenv("SEMANTIC_ROUTER_DECISION_MAP", "") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("sr_decision_map_malformed", error=str(e))
        return {}
    if not isinstance(obj, dict):
        logger.warning("sr_decision_map_not_object")
        return {}
    # keep only str->str entries
    return {str(k): str(v) for k, v in obj.items() if isinstance(v, (str,))}


def normalize_decision(
    decision_name: str,
    *,
    decision_map: dict,
    is_known_model: Callable[[str], bool],
) -> Optional[str]:
    """PURE: map an SR decision name to a registry model_id, or None if unmappable.

      1. explicit map entry wins;
      2. else identity — a decision name that is already a known registry model id
         maps to itself;
      3. else None (unmapped ⇒ caller fails open with an alert).

    The result is NOT trusted as servable — it still passes the downstream
    allowlist/servability gate exactly like a client pin. This only decides
    *which name* the reserve considers, never whether it is allowed."""
    if not decision_name:
        return None
    mapped = decision_map.get(decision_name)
    if mapped:
        return mapped
    if is_known_model(decision_name):
        return decision_name
    return None


def make_normalizer(is_known_model: Callable[[str], bool]) -> Callable[[str], Optional[str]]:
    """Build the (decision_name) -> model_id|None closure decide() passes to the
    eval client, binding the process's current decision map + the registry
    membership test."""
    dm = _load_map()
    return lambda name: normalize_decision(name, decision_map=dm, is_known_model=is_known_model)


def validate_map_against_registry(
    decision_map: dict,
    *,
    is_priced_enabled: Callable[[str], bool],
) -> list[str]:
    """CI-gate helper (PURE): return the list of map VALUES that are NOT a
    priced+enabled registry model. Empty ⇒ the map is deployable. A non-empty
    result must fail the build (an SR decision could resolve to a model the ledger
    cannot price, which would break the reserve upper bound)."""
    bad = []
    for _name, model_id in decision_map.items():
        if not is_priced_enabled(model_id):
            bad.append(model_id)
    return sorted(set(bad))
