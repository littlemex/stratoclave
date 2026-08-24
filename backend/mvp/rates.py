"""The rate value type, and validation for any table that claims to hold rates.

`Rate` lives here rather than in `mvp.pricing` so that a price source can be typed
and validated without importing the charging module. `pricing` imports
`price_sources`, which needs `Rate`; keeping `Rate` in `pricing` meant the source
module reached back into a half-initialised `pricing` and only worked because the
class happened to be defined above the line that triggered the import. This module
has no intra-package imports, so that ordering hazard is structural gone rather
than merely dormant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def no_duplicate_keys(pairs):
    """`object_pairs_hook` that rejects a repeated key instead of taking the last.

    `json.load` silently keeps the last occurrence, so a botched merge that leaves
    `"opus"` twice would make the charged rate depend on document order — the same
    ambiguity the model registry rejects for duplicate ids.
    """
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


RATE_FIELDS = (
    "input_per_mtok_microusd",
    "output_per_mtok_microusd",
    "cache_read_per_mtok_microusd",
    "cache_write_per_mtok_microusd",
)


@dataclass(frozen=True)
class Rate:
    """Per-MTok rates in micro-USD for one pricing key."""

    input_per_mtok_microusd: int
    output_per_mtok_microusd: int
    cache_read_per_mtok_microusd: int
    cache_write_per_mtok_microusd: int


def validate_rate_table(table: object, *, origin: str) -> dict[str, Rate]:
    """Return `table` as a checked `{key: Rate}` map, or raise `ValueError`.

    Applied to EVERY layer, including a plugin's return value. A source is
    third-party code from this module's point of view: an unchecked `{"opus": None}`
    or a negative rate would otherwise reach the charging arithmetic, where the
    first symptom is a wrong invoice or a 500 on the request path.
    """
    if not isinstance(table, Mapping):
        raise ValueError(f"{origin}: rate table must be a mapping, got {type(table).__name__}")
    checked: dict[str, Rate] = {}
    for key, rate in table.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{origin}: rate key {key!r} must be a non-empty string")
        if not isinstance(rate, Rate):
            raise ValueError(
                f"{origin}: rates[{key!r}] must be a Rate, got {type(rate).__name__}"
            )
        for field in RATE_FIELDS:
            value = getattr(rate, field)
            # bool is an int subclass; True would silently price a token class at 1.
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"{origin}: rates[{key!r}].{field} must be a non-negative integer, "
                    f"got {value!r}"
                )
        checked[key] = rate
    return checked
