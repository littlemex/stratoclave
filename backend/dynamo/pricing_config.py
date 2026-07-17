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

    def get_rates_for_version(self, version: str, pricing_key: str):
        """Return the rate-row item for one (version, pricing_key), or None.

        Rating (Layer 5) freezes the exact rate a reservation was admitted at, by
        version — so this reads ONE immutable row (a version's rows never change
        after `set_rates` flips CURRENT). No TTL cache is needed: the row is
        immutable, so the caller (mvp.pricing) caches it forever by
        (version, pricing_key).

        This is the boto3 RESOURCE Table, so the returned Item is a high-level
        dict of Python types (numbers arrive as `Decimal`) — the caller `int()`s
        the rate fields. `ConsistentRead=True`: a snapshot taken just after
        `set_rates` flips CURRENT must not miss the freshly-written row (a stale
        read would drop rating to a mislabeled default — Fable review M5). The
        row is immutable so this strong read happens at most once per version.
        """
        resp = self._table.get_item(
            Key={"pk": _PK, "sk": f"__ratever__{version}__{pricing_key}"},
            ConsistentRead=True,
        )
        return resp.get("Item")

    # ----- write (admin) -----
    #
    # IMMUTABLE-VERSION CONTRACT (Layer 5 rating): a version's rate rows must
    # NEVER change once written, and rows must NEVER be deleted. Rating freezes
    # the rate a charge was computed at BY VALUE on the ledger terminal (the
    # normative dispute evidence), but PricingConfig is the secondary record and
    # must stay reproducible too. There is deliberately NO delete API here; to
    # change a price, write a NEW version and flip CURRENT. Immutability is
    # enforced at the DB layer below by `attribute_not_exists(sk)` on each row
    # Put — NOT by IAM (IAM cannot express "create-only PutItem", Fable review M3).
    def set_rates(self, *, version: str, rates: dict, costs: Optional[dict] = None) -> None:
        """Write a full rate set under a NEW `version` and flip CURRENT to it.

        `rates` maps pricing_key -> object exposing the four per-MTok integer
        fields (a `mvp.pricing.Rate` or any duck-typed equivalent). The rows
        are written first (each gated by `attribute_not_exists(sk)` so an
        existing version can NEVER be silently overwritten — the immutable
        contract is DB-enforced), then the pointer, so a reader never sees
        CURRENT pointing at a half-written version.

        `costs` (Layer 5-d, optional) maps pricing_key -> object with the same
        four per-MTok fields expressing the PROVIDER COST (Bedrock's price to us).
        When present for a key, its four values are written as `cost_*` columns on
        that key's row (record-only — they never affect the charged amount, only
        the frozen provider_cost/margin on the ledger). A key absent from `costs`
        keeps null cost columns ("unknown", distinct from zero). Costs may exceed
        the charged rate (loss-leader) — no margin>=0 constraint.

        `version` MUST be fresh and well-formed: reusing an existing version, or
        using the reserved `builtin` sentinel, or a string containing the `__`
        delimiter, is rejected (raises ValueError) — these would corrupt version
        labels, the sentinel check, or the composite sort key.
        """
        import re

        from mvp.pricing import RESERVED_VERSIONS

        if not version or version in RESERVED_VERSIONS:
            raise ValueError(f"reserved/empty pricing version: {version!r}")
        # `__` is the sk delimiter; leading/trailing `_` would make version/key
        # boundaries ambiguous (Fable review-2 N4), so forbid `_` at the edges.
        if (
            "__" in version
            or version.startswith("_")
            or version.endswith("_")
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", version)
        ):
            raise ValueError(f"malformed pricing version: {version!r}")
        for key in rates:
            if "__" in str(key):
                raise ValueError(f"malformed pricing_key (contains '__'): {key!r}")
        # A cost for a key not in `rates` is almost certainly a typo. Since the
        # version is immutable it could never be corrected, so reject it up front
        # (Fable L5-d review-2 L-1) rather than freeze an orphaned/unknown cost.
        if costs:
            orphan = set(costs) - set(rates)
            if orphan:
                raise ValueError(f"costs for keys not in rates: {sorted(orphan)}")

        from botocore.exceptions import ClientError

        costs = costs or {}
        for key, rate in rates.items():
            vin = int(rate.input_per_mtok_microusd)
            vout = int(rate.output_per_mtok_microusd)
            vcr = int(rate.cache_read_per_mtok_microusd)
            vcw = int(rate.cache_write_per_mtok_microusd)
            item = {
                "pk": _PK,
                "sk": f"__ratever__{version}__{key}",
                "pricing_key": key,
                "version": version,
                "input_per_mtok_microusd": vin,
                "output_per_mtok_microusd": vout,
                "cache_read_per_mtok_microusd": vcr,
                "cache_write_per_mtok_microusd": vcw,
            }
            # Optional record-only provider-cost columns (L5-d). Non-negative int.
            _cost_cols = (
                ("cost_input_per_mtok_microusd", ":ci"),
                ("cost_output_per_mtok_microusd", ":co"),
                ("cost_cache_read_per_mtok_microusd", ":ccr"),
                ("cost_cache_write_per_mtok_microusd", ":ccw"),
            )
            cost = costs.get(key)
            cost_vals: dict[str, int] = {}
            if cost is not None:
                for (col, ph), val in zip(
                    _cost_cols,
                    (cost.input_per_mtok_microusd, cost.output_per_mtok_microusd,
                     cost.cache_read_per_mtok_microusd, cost.cache_write_per_mtok_microusd),
                ):
                    iv = int(val)
                    if iv < 0:
                        raise ValueError(f"provider cost must be non-negative: {col}={iv}")
                    item[col] = iv
                    cost_vals[ph] = iv
            try:
                # IDEMPOTENT immutability (Fable review-2 N1 + L5-d H1): allow the
                # row to be (re)written iff it does not exist OR already holds the
                # SAME values — for the four CHARGE rates AND the four cost_*
                # columns. Guarding ONLY the charge rates (the earlier version)
                # let a re-`set_rates` with the same rates but different/absent
                # costs silently mutate the record-only cost, breaking the "one
                # pricing_version → one provider_cost" audit guarantee. Each cost
                # column is matched as "absent-on-both OR equal", so costs-present
                # and costs-absent are each immutable, and crash-retry with the
                # SAME payload still succeeds.
                _cost_clause = " AND ".join(
                    (
                        f"{col} = {ph}" if ph in cost_vals
                        else f"attribute_not_exists({col})"
                    )
                    for col, ph in _cost_cols
                )
                _values = {":i": vin, ":o": vout, ":cr": vcr, ":cw": vcw}
                _values.update(cost_vals)
                self._table.put_item(
                    Item=item,
                    ConditionExpression=(
                        "attribute_not_exists(sk) OR "
                        "(input_per_mtok_microusd = :i AND output_per_mtok_microusd = :o "
                        "AND cache_read_per_mtok_microusd = :cr "
                        "AND cache_write_per_mtok_microusd = :cw "
                        f"AND {_cost_clause})"
                    ),
                    ExpressionAttributeValues=_values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise ValueError(
                        f"pricing version {version!r} already exists with DIFFERENT "
                        f"rates or costs for {key!r} (immutable) — use a fresh version"
                    ) from e
                raise
        # Flip CURRENT last. Written unconditionally so a crash AFTER the rows but
        # BEFORE this flip is recoverable: re-running set_rates with the same
        # (version, rates) idempotently re-writes the rows and completes the flip.
        self._table.put_item(
            Item={"pk": _PK, "sk": _CURRENT_SK, "active_version": version}
        )
