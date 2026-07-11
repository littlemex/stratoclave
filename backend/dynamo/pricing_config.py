"""PricingConfig table: admin-editable per-model dollar rates.

Layout (single partition, `CONFIG#pricing`):

    PK = "CONFIG#pricing", SK = "CURRENT"
        -> { active_version: "<version>" }        # the pointer
    PK = "CONFIG#pricing", SK = "__ratever__<version>__<pricing_key>"
        -> { pricing_key, version, input_per_mtok_microusd,
             output_per_mtok_microusd, cache_read_per_mtok_microusd,
             cache_write_per_mtok_microusd }

The rate rows share the `__ratever__<version>__` SK prefix so that all rows
for one version are a single `begins_with` query.

Versioning is copy-on-write: an admin writes a full set of rows under a new
version string, then flips `CURRENT.active_version` to it in one update. The
pricing module reads only `CURRENT` on its refresh tick and pulls the rows for
a version once, so hot-reload costs one point read in the steady state.

All money is integer micro-USD; this module never introduces a float.
"""
from __future__ import annotations

from typing import Optional

from boto3.dynamodb.conditions import Key

from .client import get_dynamodb_resource, pricing_config_table_name


_PK = "CONFIG#pricing"
_CURRENT_SK = "CURRENT"


class PricingConfigRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or pricing_config_table_name()
        )

    # ----- read -----
    def current_version(self) -> Optional[str]:
        """Return the active pricing version, or None if none is set."""
        resp = self._table.get_item(Key={"pk": _PK, "sk": _CURRENT_SK})
        item = resp.get("Item")
        if not item:
            return None
        version = item.get("active_version")
        return str(version) if version else None

    def load_rates(self, version: str):
        """Return {pricing_key: Rate} for a version.

        Imported lazily to avoid a circular import with `mvp.pricing` (which
        imports this module). Rows missing a field default that field to 0.
        """
        from mvp.pricing import Rate

        resp = self._table.query(
            KeyConditionExpression=Key("pk").eq(_PK)
            & Key("sk").begins_with(f"__ratever__{version}__")
        )
        rates: dict[str, Rate] = {}
        for item in resp.get("Items", []):
            key = item.get("pricing_key")
            if not key:
                continue
            rates[str(key)] = Rate(
                input_per_mtok_microusd=int(item.get("input_per_mtok_microusd", 0)),
                output_per_mtok_microusd=int(item.get("output_per_mtok_microusd", 0)),
                cache_read_per_mtok_microusd=int(
                    item.get("cache_read_per_mtok_microusd", 0)
                ),
                cache_write_per_mtok_microusd=int(
                    item.get("cache_write_per_mtok_microusd", 0)
                ),
            )
        return rates

    # ----- write (admin) -----
    def set_rates(self, *, version: str, rates: dict) -> None:
        """Write a full rate set under `version` and flip CURRENT to it.

        `rates` maps pricing_key -> object exposing the four per-MTok integer
        fields (a `mvp.pricing.Rate` or any duck-typed equivalent). The rows
        are written first, then the pointer, so a reader never sees CURRENT
        pointing at a half-written version.
        """
        for key, rate in rates.items():
            self._table.put_item(
                Item={
                    "pk": _PK,
                    "sk": f"__ratever__{version}__{key}",
                    "pricing_key": key,
                    "version": version,
                    "input_per_mtok_microusd": int(rate.input_per_mtok_microusd),
                    "output_per_mtok_microusd": int(rate.output_per_mtok_microusd),
                    "cache_read_per_mtok_microusd": int(
                        rate.cache_read_per_mtok_microusd
                    ),
                    "cache_write_per_mtok_microusd": int(
                        rate.cache_write_per_mtok_microusd
                    ),
                }
            )
        self._table.put_item(
            Item={"pk": _PK, "sk": _CURRENT_SK, "active_version": version}
        )
